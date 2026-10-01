// seeknanousb.c — Seek Nano native WinUSB transport (proper driver-grade data path).
//
// Exposed API (C ABI, consumed via ctypes):
//   int  SN_open(void);                 find USB\VID_289D&PID_0011 via SetupDI / INF GUID
//   void SN_close(void);
//   int  SN_stream_start(void);         full handshake (phases 1..4 from the RE)
//   int  SN_stream_stop(void);
//   int  SN_get_frame(unsigned char*);  request + blocking 177,840 B bulk read
//   int  SN_chipid(char*, int cap);     "CQ-DBAX"
//   int  SN_serial(char*, int cap);
//   int  SN_fwver(char*, int cap);
//
// Build:  cl /nologo /W4 /LD seeknanousb.c /Fe:seeknanousb.dll /link winusb.lib setupapi.lib

#define WIN32_LEAN_AND_MEAN
#define _CRT_SECURE_NO_WARNINGS
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <windows.h>
#include <winusb.h>
#include <usbiodef.h>
#include <setupapi.h>

#define SN_VID      0x289D
#define SN_PID      0x0011
static const GUID SN_DEVGUID =
{ 0x1C2BD42A, 0x95E0, 0x4D26, { 0x9A, 0x79, 0xE1, 0xF2, 0xC8, 0x6A, 0xC3, 0x89 } };

#define FRAME_BYTES 177840
#define CHUNK       6840
#define BULK_EP     0x81

static WINUSB_INTERFACE_HANDLE g_winusb = NULL;
static HANDLE g_dev = INVALID_HANDLE_VALUE;
static int g_streaming = 0;

static void le32(unsigned char* d, unsigned v)
{
    d[0] = (unsigned char)(v & 0xFF);
    d[1] = (unsigned char)(v >> 8);
    d[2] = (unsigned char)(v >> 16);
    d[3] = (unsigned char)(v >> 24);
}

static int ctrl_out(unsigned char request, const unsigned char* data, unsigned short len)
{
    if (!g_winusb)
        return -1;
    WINUSB_SETUP_PACKET p;
    ZeroMemory(&p, sizeof(p));
    p.RequestType = 0x40;
    p.Request = request;
    p.Length = len;
    ULONG sent = 0;
    if (!WinUsb_ControlTransfer(g_winusb, p, (unsigned char*)data, len, &sent, NULL))
        return -1;
    return (int)sent;
}

static int ctrl_in(unsigned char request, unsigned short len, unsigned char* out)
{
    if (!g_winusb)
        return -1;
    WINUSB_SETUP_PACKET p;
    ZeroMemory(&p, sizeof(p));
    p.RequestType = 0xC0;
    p.Request = request;
    p.Length = len;
    ULONG got = 0;
    if (!WinUsb_ControlTransfer(g_winusb, p, out, len, &got, NULL))
        return -1;
    return (int)got;
}

__declspec(dllexport) void SN_close(void)
{
    if (g_winusb) {
        WinUsb_Free(g_winusb);
        g_winusb = NULL;
    }
    if (g_dev != INVALID_HANDLE_VALUE) {
        CloseHandle(g_dev);
        g_dev = INVALID_HANDLE_VALUE;
    }
    g_streaming = 0;
}

__declspec(dllexport) int SN_open(void)
{
    /* re-entry guard: a retry after a transient dropout leaked the previous
     * enumeration with no way to close it */
    if (g_winusb || g_dev != INVALID_HANDLE_VALUE)
        return -8;
    HDEVINFO devInfo = SetupDiGetClassDevsW(&SN_DEVGUID, NULL, NULL,
                        DIGCF_PRESENT | DIGCF_DEVICEINTERFACE);
    if (devInfo == INVALID_HANDLE_VALUE)
        return -1;
    SP_DEVICE_INTERFACE_DATA di;
    di.cbSize = sizeof(di);
    if (!SetupDiEnumDeviceInterfaces(devInfo, NULL, (GUID*)&SN_DEVGUID, 0, &di)) {
        SetupDiDestroyDeviceInfoList(devInfo);
        return -2;    /* no device bound to our INF */
    }
    DWORD need = 0;
    if (!SetupDiGetDeviceInterfaceDetailW(devInfo, &di, NULL, 0, &need, NULL) &&
        GetLastError() != ERROR_INSUFFICIENT_BUFFER) {
        SetupDiDestroyDeviceInfoList(devInfo);
        return -3;
    }
    PSP_DEVICE_INTERFACE_DETAIL_DATA_W det =
        (PSP_DEVICE_INTERFACE_DETAIL_DATA_W)HeapAlloc(GetProcessHeap(), 0, need);
    if (!det) {
        SetupDiDestroyDeviceInfoList(devInfo);
        return -3;
    }
    det->cbSize = sizeof(SP_DEVICE_INTERFACE_DETAIL_DATA_W);
    if (!SetupDiGetDeviceInterfaceDetailW(devInfo, &di, det, need, NULL, NULL)) {
        HeapFree(GetProcessHeap(), 0, det);
        SetupDiDestroyDeviceInfoList(devInfo);
        return -4;
    }
    g_dev = CreateFileW(det->DevicePath,
                        GENERIC_WRITE | GENERIC_READ,
                        FILE_SHARE_READ | FILE_SHARE_WRITE,
                        NULL, OPEN_EXISTING,
                        FILE_ATTRIBUTE_NORMAL, NULL);
    HeapFree(GetProcessHeap(), 0, det);
    SetupDiDestroyDeviceInfoList(devInfo);
    if (g_dev == INVALID_HANDLE_VALUE)
        return -5;
    if (!WinUsb_Initialize(g_dev, &g_winusb)) {
        CloseHandle(g_dev);
        g_dev = INVALID_HANDLE_VALUE;
        return -6;
    }
    /* bulk-in pipe timeout so a stalled camera does not hang the caller forever */
    DWORD t = 500;
    if (!WinUsb_SetPipePolicy(g_winusb, BULK_EP, PIPE_TRANSFER_TIMEOUT,
                              sizeof(DWORD), &t)) {
        SN_close();
        return -7;
    }
    g_streaming = 0;
    return 0;
}

static int phase4(void)
{
    unsigned char cfg[4] = { 0xfc, 0x00, 0x04, 0x00 };
    unsigned char on[2] = { 0x01, 0x00 };
    unsigned char st[2];
    if (ctrl_out(0x37, cfg, 4) < 0)
        return -1;
    Sleep(5);
    if (ctrl_out(0x3C, on, 2) < 0)
        return -2;
    if (ctrl_in(0x3D, 2, st) < 0)
        return -3;
    g_streaming = 1;
    return 0;
}

__declspec(dllexport) int SN_stream_start(void)
{
    if (!g_winusb)
        return -1;
    unsigned char z2[2] = { 0, 0 };
    unsigned char st[2];
    unsigned char ipm[2] = { 0x08, 0x00 };
    /* phase 1 */
    if (ctrl_out(0x54, z2, 2) < 0)  return -10;
    if (ctrl_out(0x3C, z2, 2) < 0)  return -11;
    if (ctrl_in(0x3D, 2, st) < 0)   return -12;
    if (ctrl_out(0x3E, ipm, 2) < 0) return -13;
    return phase4();
}

static void frame_request(void)
{
    unsigned char req[4];
    le32(req, 88920);          /* 342*260 raw samples per frame */
    ctrl_out(0x53, req, 4);
}

__declspec(dllexport) int SN_get_frame(unsigned char* out)
{
    if (!g_winusb || !g_streaming)
        return -1;
    frame_request();
    unsigned long total = 0;
    while (total < FRAME_BYTES) {
        unsigned long got = 0;
        if (!WinUsb_ReadPipe(g_winusb, BULK_EP, out + total,
                             (unsigned long)(FRAME_BYTES - total), &got, NULL))
            return -2;
        if (got == 0)
            break;
        total += got;
    }
    if (total != FRAME_BYTES)
        return -3;
    /* same magic the libusb transport validates from viewer.py; returning
     * a full buffer of garbage made the length-only check pass it */
    if (out[0] != 0x79 || out[1] != 0x05)
        return -4;
    return (int)total;
}

__declspec(dllexport) int SN_stream_stop(void)
{
    if (!g_winusb)
        return -1;
    unsigned char z2[2] = { 0, 0 };
    ctrl_out(0x3C, z2, 2);
    g_streaming = 0;
    return 0;
}

static void copyc16(const unsigned char* d, int n, char* out, int cap)
{
    int j = 0;
    for (int i = 0; i < n && j + 1 < cap; i++)
        if (d[i] >= 0x20 && d[i] < 0x7F)
            out[j++] = (char)d[i];
    out[j] = 0;
}

__declspec(dllexport) int SN_chipid(char* out, int cap)
{
    unsigned char cursor[6] = { 0x08, 0x00, 0x02, 0x06, 0x00, 0x00 };
    unsigned char buf[16];
    if (ctrl_out(0x56, cursor, 6) < 0)
        return -1;
    if (ctrl_in(0x58, 16, buf) < 0)
        return -2;
    copyc16(buf, 16, out, cap);
    return 0;
}

__declspec(dllexport) int SN_serial(char* out, int cap)
{
    unsigned char sel[2] = { 0x17, 0x00 };
    unsigned char buf[64];
    if (ctrl_out(0x55, sel, 2) < 0)
        return -1;
    if (ctrl_in(0x4E, 64, buf) < 0)
        return -2;
    copyc16(buf, 16, out, cap);          /* first chunk: serial  */
    return 0;
}

__declspec(dllexport) int SN_fwver(char* out, int cap)
{
    unsigned char sel[2] = { 0x15, 0x00 };
    unsigned char buf[64];
    int j = 0;
    if (ctrl_out(0x55, sel, 2) < 0)
        return -1;
    if (ctrl_in(0x4E, 64, buf) < 0)
        return -2;
    for (int i = 0; i < 24 && j + 3 < cap; i++)
        j += wsprintfA(out + j, "%02x ", buf[i]);
    if (j > cap - 1)
        j = cap - 1;
    out[j] = 0;
    return 0;
}
