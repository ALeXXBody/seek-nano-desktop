// seeknanousb_libusb.c — Seek Nano native C transport via libusb-1.0.dll
// (works with the libusbK driver binding that Zadig already installs; no INF needed)
//
// API (C ABI via ctypes):
//   int  SNLB_open(void);            find/open vid 289d pid 0011, claim if0
//   void SNLB_close(void);
//   int  SNLB_stream_start(void);    full handshake replay (phases 1..4)
//   int  SNLB_stream_stop(void);
//   int  SNLB_get_frame(unsigned char* buf);   fills 177,840 B, one call per frame
//   int  SNLB_chipid(char* out, int cap);
//   int  SNLB_serial(char* out, int cap);
//   int  SNLB_fwver(char* out, int cap);
//
// Build (MSVC):
//   cl /nologo /W4 /LD seeknanousb_libusb.c /Fe:seeknanousb_libusb.dll /link kernel32.lib user32.lib
// (symbol resolution is done dynamically via LoadLibrary on libusb-1.0.dll which
//  PyInstaller already bundles as seeknanousb.dll dependencies)

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdio.h>

typedef void* libusb_device_handle;
typedef void* libusb_context;

#define LIBUSB_CTRL_IN   0xC0

static libusb_context g_ctx = NULL;
static libusb_device_handle g_dev = NULL;
static int g_claimed = 0;
static int g_streaming = 0;

// ---- dynamic libusb symbols ----
static int  (*p_libusb_init)(libusb_context*);
static void (*p_libusb_exit)(libusb_context);
static libusb_device_handle (*p_libusb_open_device_with_vid_pid)(libusb_context, unsigned short, unsigned short);
static int  (*p_libusb_claim_interface)(libusb_device_handle, int);
static int  (*p_libusb_release_interface)(libusb_device_handle, int);
static int  (*p_libusb_bulk_transfer)(libusb_device_handle, unsigned char, unsigned char*, int, int*, unsigned);
static int  (*p_libusb_control_transfer)(libusb_device_handle, unsigned char, unsigned char, unsigned short, unsigned short, unsigned char*, unsigned short, unsigned);
static int  (*p_libusb_clear_halt)(libusb_device_handle, unsigned char);
static void (*p_libusb_close)(libusb_device_handle);
static int  (*p_libusb_set_configuration)(libusb_device_handle, int);

static struct { const char* name; void** pp; } syms[] = {
    { "libusb_init",                        (void**)&p_libusb_init },
    { "libusb_exit",                        (void**)&p_libusb_exit },
    { "libusb_open_device_with_vid_pid",    (void**)&p_libusb_open_device_with_vid_pid },
    { "libusb_claim_interface",             (void**)&p_libusb_claim_interface },
    { "libusb_release_interface",           (void**)&p_libusb_release_interface },
    { "libusb_bulk_transfer",               (void**)&p_libusb_bulk_transfer },
    { "libusb_control_transfer",            (void**)&p_libusb_control_transfer },
    { "libusb_clear_halt",                  (void**)&p_libusb_clear_halt },
    { "libusb_close",                       (void**)&p_libusb_close },
    { "libusb_set_configuration",            (void**)&p_libusb_set_configuration },
};

#define SYM_N (sizeof(syms)/sizeof(syms[0]))

static HMODULE g_libusb = NULL;

static int load_libusb(void)
{
    if (g_libusb) return 0;
    /* resolve libusb-1.0.dll from OUR OWN module directory first, never a
     * bare PATH/cwd search: this dll is shipped next to an unsigned exe in a
     * user-downloaded folder, so a planted libusb-1.0.dll on PATH wins the
     * search order. GetModuleHandleW(NULL) is the exe; the dll itself sits
     * beside it in the dev bundle, so the exe dir is the right first shot. */
    wchar_t dir[MAX_PATH];
    HMODULE self = NULL;
    /* FROM_ADDRESS on a function in this file = our own module */
    GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS,
                       (LPCWSTR)&load_libusb, &self);
    DWORD n = self ? GetModuleFileNameW(self, dir, MAX_PATH) : 0;
    if (n > 0 && n < MAX_PATH) {
        wchar_t full[MAX_PATH];
        const wchar_t* last = dir;
        for (const wchar_t* p = dir; *p; p++)
            if (*p == L'\\' || *p == L'/') last = p;
        size_t dl = (size_t)(last - dir);
        lstrcpynW(full, dir, (int)dl + 1);
        lstrcatW(full, L"\\libusb-1.0.dll");
        g_libusb = LoadLibraryW(full);
    }
    if (!g_libusb)
        g_libusb = LoadLibraryW(L"libusb-1.0.dll");
    if (!g_libusb) return -1;
    for (unsigned i = 0; i < SYM_N; i++)
        if (!(*syms[i].pp = (void*)GetProcAddress(g_libusb, syms[i].name))) {
            /* a partial symbol table must never look "loaded": a retry with
             * g_libusb non-NULL would call a NULL function pointer */
            FreeLibrary(g_libusb);
            g_libusb = NULL;
            return -2;
        }
    if (p_libusb_init(&g_ctx) != 0) {
        FreeLibrary(g_libusb);
        g_libusb = NULL;
        return -3;
    }
    return 0;
}

__declspec(dllexport) int SNLB_open(void)
{
    /* re-entry guard: overwrite a live handle would leak it */
    if (g_dev) return -8;
    int r = load_libusb();
    if (r) return r;
    g_dev = p_libusb_open_device_with_vid_pid(g_ctx, 0x289D, 0x0011);
    if (!g_dev) return -1;
    p_libusb_set_configuration(g_dev, 1);
    r = p_libusb_claim_interface(g_dev, 0);
    if (r != 0) return -2;
    g_claimed = 1;
    g_streaming = 0;
    return 0;
}

static int c_out(unsigned char request, unsigned int length, const unsigned char* data)
{
    return p_libusb_control_transfer(g_dev, 0x40, request, 0, 0,
             (unsigned char*)data, (unsigned short)length, 1250);
}

static int c_in(unsigned char request, unsigned int length, unsigned char* buf)
{
    return p_libusb_control_transfer(g_dev, LIBUSB_CTRL_IN, request, 0, 0,
             buf, (unsigned short)length, 1250);
}

__declspec(dllexport) int SNLB_chipid(char* out, int cap)
{
    unsigned char cursor[6] = { 0x08, 0x00, 0x02, 0x06, 0x00, 0x00 };
    unsigned char buf[16];
    int r;
    if (c_out(0x56, 6, cursor) < 0) return -1;
    r = c_in(0x58, 16, buf);
    if (r < 0) return -2;
    int j = 0;
    for (int i = 0; i < r && j + 1 < cap; i++)
        if (buf[i] >= 0x20 && buf[i] < 0x7F) out[j++] = (char)buf[i];
    out[j] = 0;
    return 0;
}

__declspec(dllexport) int SNLB_serial(char* out, int cap)
{
    unsigned char sel[2] = { 0x17, 0x00 };
    unsigned char buf[64];
    if (c_out(0x55, 2, sel) < 0) return -1;
    int r = c_in(0x4E, 64, buf);
    if (r < 0) return -2;
    int j = 0;
    for (int i = 0; i < 16 && j + 1 < cap; i++)
        if (buf[i] >= 0x20 && buf[i] < 0x7F) out[j++] = (char)buf[i];
    out[j] = 0;
    return 0;
}

__declspec(dllexport) int SNLB_fwver(char* out, int cap)
{
    unsigned char sel[2] = { 0x15, 0x00 };
    unsigned char buf[64];
    if (c_out(0x55, 2, sel) < 0) return -1;
    int r = c_in(0x4E, 64, buf);
    if (r < 0) return -2;
    int j = 0;
    for (int i = 0; i < 24 && j + 3 < cap; i++)
        j += wsprintfA(out + j, "%02x ", buf[i]);
    if (j > cap - 1) j = cap - 1;
    out[j] = 0;
    return 0;
}

static void le32(unsigned char* d, unsigned v)
{
    d[0] = v & 0xFF; d[1] = v >> 8; d[2] = v >> 16; d[3] = v >> 24;
}

static void nlog(const char* m)
{
    HANDLE h = CreateFileA("seeknano_native.log", FILE_APPEND_DATA, FILE_SHARE_READ,
                           NULL, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (h == INVALID_HANDLE_VALUE) return;
    DWORD n = 0;
    WriteFile(h, m, (DWORD)lstrlenA(m), &n, NULL);
    WriteFile(h, "\r\n", 2, &n, NULL);
    CloseHandle(h);
}

__declspec(dllexport) int SNLB_stream_start(void)
{
    if (!g_claimed) return -1;
    unsigned char z2[2]  = { 0, 0 };
    unsigned char st[2];
    unsigned char cursor[6];
    unsigned char sel[2];
    unsigned char page[64];
    unsigned char imgproc[2] = { 0x08, 0x00 };
    unsigned char cfg[4]  = { 0xfc, 0x00, 0x04, 0x00 };
    unsigned char on[2]   = { 0x01, 0x00 };
    int r, i;

    nlog("start: phase 1");
    if (c_out(0x54, 2, z2)  < 0) return -10;
    if (c_out(0x3C, 2, z2)  < 0) return -11;
    if (c_in(0x3D, 2, st)   < 0) return -12;
    if (c_out(0x3E, 2, imgproc) < 0) return -13;

    nlog("start: phase 2 identity");
    cursor[0] = 0x08; cursor[1] = 0x00; cursor[2] = 0x02; cursor[3] = 0x06; cursor[4] = 0x00; cursor[5] = 0x00;
    if (c_out(0x56, 6, cursor) < 0) return -20;
    c_in(0x58, 16, page);                    /* chip id */
    c_in(0x4E, 4,  page);
    sel[0] = 0x17; sel[1] = 0x00;
    if (c_out(0x55, 2, sel) < 0) return -21;
    c_in(0x4E, 64, page);                    /* serial + part number */
    c_in(0x4E, 4,  page);
    c_in(0x36, 12, page);                    /* fw info */
    sel[0] = 0x15; sel[1] = 0x00;
    if (c_out(0x55, 2, sel) < 0) return -22;
    c_in(0x4E, 64, page);
    c_in(0x4E, 4,  page);
    c_in(0x36, 12, page);

    nlog("start: phase 3 rom dump 0x00-0x0A00");
    for (i = 0; i < 11; i++) {
        cursor[0] = 0x20; cursor[1] = 0x00;
        cursor[2] = (unsigned char)((i * 64 >> 8) & 0xFF);
        cursor[3] = (unsigned char)((i * 64) & 0xFF);
        cursor[4] = 0; cursor[5] = 0;
        if (c_out(0x56, 6, cursor) < 0) return -30;
        r = c_in(0x58, 64, page);
        if (r < 0) return -31;
    }
    sel[0] = 0x15; sel[1] = 0x00;
    if (c_out(0x55, 2, sel) < 0) return -23;
    c_in(0x4E, 64, page);
    c_in(0x35, 4, st);

    nlog("start: phase 4");
    if (c_out(0x37, 4, cfg) < 0) return -14;
    if (c_out(0x3C, 2, on)  < 0) return -15;
    if (c_in(0x3D, 2, st)   < 0) return -16;
    p_libusb_clear_halt(g_dev, 0x81);
    g_streaming = 1;
    nlog("streaming");
    return 0;
}

__declspec(dllexport) int SNLB_get_frame(unsigned char* out)
{
    if (!g_dev || !g_streaming) return -1;
    unsigned char req[4];
    int total = 0, kick;

    le32(req, 88920);
    /* Mirror pump_once() in pcdriver/seek_nano.py: poll until the 0x35
     * readiness readback says a frame is queued, then read. The old fixed
     * 200-iteration loop ran ~400 control transfers per frame and returned
     * early readiness yet kept going - pure latency. */
    for (kick = 0; kick < 200; kick++) {
        if (c_out(0x53, 4, req) < 0) return -5;
        if (c_in(0x35, 4, req) < 0) return -6;
        if (req[0] != 0 || req[1] != 0)
            break;
    }
    /* camera queues the frame around now; read it in device chunks */
    while (total < 177840) {
        int n = 0;
        int r = p_libusb_bulk_transfer(g_dev, 0x81, out + total,
                                       6840, &n, 400);
        if (r != 0) return -2;
        if (n == 0) break;
        total += n;
    }
    if (total != 177840) return -3;
    if (out[0] != 0x79 || out[1] != 0x05) return -4;
    return total;
}

__declspec(dllexport) void SNLB_stream_stop(void)
{
    if (!g_dev) return;
    unsigned char z2[2] = { 0, 0 };
    c_out(0x3C, 2, z2);
    g_streaming = 0;
}

__declspec(dllexport) void SNLB_close(void)
{
    if (!g_dev) return;
    p_libusb_release_interface(g_dev, 0);
    p_libusb_close(g_dev);
    g_dev = NULL;
    g_claimed = 0;
    g_streaming = 0;
}
