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
static int  (*p_libusb_set_auto_detach_kernel_driver)(libusb_device_handle, int);

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
    { "libusb_set_auto_detach_kernel_driver",(void**)&p_libusb_set_auto_detach_kernel_driver },
};

#define SYM_N (sizeof(syms)/sizeof(syms[0]))

static HMODULE g_libusb = NULL;

static int load_libusb(void)
{
    if (g_libusb) return 0;
    g_libusb = LoadLibraryW(L"libusb-1.0.dll");
    if (!g_libusb) return -1;
    for (unsigned i = 0; i < SYM_N; i++)
        if (!(*syms[i].pp = (void*)GetProcAddress(g_libusb, syms[i].name)))
            return -2;
    if (p_libusb_init(&g_ctx) != 0)
        return -3;
    return 0;
}

__declspec(dllexport) int SNLB_open(void)
{
    int r = load_libusb();
    if (r) return r;
    g_dev = p_libusb_open_device_with_vid_pid(g_ctx, 0x289D, 0x0011);
    if (!g_dev) return -1;
    p_libusb_set_auto_detach_kernel_driver(g_dev, 1);   /* best effort */
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
    for (int i = 0; i < 24 && j + 2 < cap; i++)
        j += wsprintfA(out + j, "%02x ", buf[i]);
    out[j] = 0;
    return 0;
}

static void le32(unsigned char* d, unsigned v)
{
    d[0] = v & 0xFF; d[1] = v >> 8; d[2] = v >> 16; d[3] = v >> 24;
}

__declspec(dllexport) int SNLB_stream_start(void)
{
    if (!g_claimed) return -1;
    unsigned char z2[2] = { 0, 0 };
    unsigned char st[2];
    if (c_out(0x54, 2, z2)  < 0) return -10;   /* TARGET_PLATFORM (as phone used ilk 0)   */
    if (c_out(0x3C, 2, z2)  < 0) return -11;   /* SET_OPERATION_MODE = 0                  */
    if (c_in(0x3D, 2, st)   < 0) return -12;
    unsigned char imgproc[2] = { 0x08, 0x00 };
    if (c_out(0x3E, 2, imgproc) < 0) return -13;
    Sleep(5);
    unsigned char cfg[4]  = { 0xfc, 0x00, 0x04, 0x00 };
    unsigned char on[2]   = { 0x01, 0x00 };
    if (c_out(0x37, 4, cfg) < 0) return -14;
    if (c_out(0x3C, 2, on)  < 0) return -15;
    if (c_in(0x3D, 2, st)   < 0) return -16;
    p_libusb_clear_halt(g_dev, 0x81);
    g_streaming = 1;
    return 0;
}

__declspec(dllexport) int SNLB_get_frame(unsigned char* out)
{
    if (!g_dev || !g_streaming) return -1;
    unsigned char req[4];
    le32(req, 88920);
    c_out(0x53, 4, req);
    int total = 0;
    while (total < 177840) {
        int n = 0;
        int r = p_libusb_bulk_transfer(g_dev, 0x81, out + total,
                                       177840 - total, &n, 400);
        if (r != 0) return -2;             /* timeout or pipe error */
        if (n == 0) break;
        total += n;
    }
    return (total == 177840) ? total : -3;
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
}
