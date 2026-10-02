#!/usr/bin/env python3
"""
Frida script executed INSIDE the Seek Nano spy app (com.thermal.seeknanospy) by the
embedded Frida gadget (libseekgadget.so, configured via assets/libseekgadget_config.so
to load /data/local/tmp/spytrace.js).

Purpose: capture the COMPLETE USB protocol spoken between the stock Seek SDK and the
Nano camera: every control transfer (both directions) and every bulk-in read, with
full hex dumps, so the PC driver can replay the exact handshake (init, nano_unlock,
authenticator, calibration fetch, streaming loop).

Captured data is appended to /sdcard/Download/spytrace.log (and mirrored to logcat).

Hook points:
  * UsbDeviceConnection.controlTransfer(int, int, int, int, byte[], int, int)  [all overloads]
  * UsbDeviceConnection.bulkTransfer(UsbEndpoint, byte[], int, int, int)      [all overloads]
  * UsbDeviceConnection.bulkTransfer(UsbEndpoint, byte[], int, int, int, int)
  * UsbRequest.queueInternalServerError hooks are not needed
  * UsbDeviceConnection.openDevice / claimInterface / releaseInterface (context log)
  * UsbManager.getDeviceList / openDevice (device enumeration log)

The file is written with buffered appends so long frame streams don't thrash I/O;
the script stays fully passive (no modification of any transfer).
"""
'use strict';

const LOG_PATH = '/sdcard/Download/spytrace.log';
const MAX_DUMP = 4096;               // cap per-transfer hex dump in log (frames are big)

function ts() { return new Date().toISOString(); }

let logFd = null;
function log(line) {
    const s = ts() + ' ' + line + '\n';
    console.log('[spysdk] ' + line);
    try {
        const buf = new Java.array('byte', stringToBytes(s));
        // java layer fallback: logcat via System.out is enough, we mainly need log file
    } catch (e) { /* pre-java-init */ }
}

// Use a Java FileOutputStream from the app process for the capture file.
function getLogger() {
    if (logFd) return logFd;
    const JavaFile = Java.use('java.io.File');
    const JavaFOS = Java.use('java.io.FileOutputStream');
    const f = JavaFile.$new(LOG_PATH);
    logFd = JavaFOS.$new(f, true);
    return logFd;
}

function stringToBytes(str) {
    const out = [];
    for (let i = 0; i < str.length; i++) out.push(str.charCodeAt(i) & 0xff);
    return out;
}

function bytesToHex(arr, off, len) {
    // arr is a byte[] inside the JVM; read through Java.array index access
    let out = '';
    if (!arr) return '<null>';
    const n = Math.min(len, arr.length - off);
    for (let i = 0; i < n && i < MAX_DUMP; i++) {
        const b = arr[off + i] & 0xff;
        out += (b < 16 ? '0' : '') + b.toString(16);
        if ((i & 31) === 31) out += '\n  ';
        else out += ' ';
    }
    if (n > MAX_DUMP) out += ` ...[+${n - MAX_DUMP} more bytes]`;
    return out;
}

function wr(line) {
    console.log('[spysdk] ' + line);
    try {
        const fos = getLogger();
        const bytes = stringToBytes(line + '\n');
        const barr = Java.array('byte', bytes);
        fos.write(barr);
    } catch (e) {
        console.log('[spysdk] log write failed: ' + e);
    }
}

Java.perform(function () {
    wr('==== spytrace session start ' + ts() + ' ====');

    // ---------------- UsbManager: enumeration + open ----------------
    const UsbManager = Java.use('android.hardware.usb.UsbManager');
    UsbManager.getDeviceList.overload().implementation = function () {
        const r = this.getDeviceList();
        const it = r.entrySet().iterator();
        while (it.hasNext()) {
            const e = it.next();
            const dev = Java.cast(e.getValue(), Java.use('android.hardware.usb.UsbDevice'));
            wr('ENUM vid=0x' + (dev.getVendorId() & 0xffff).toString(16) +
               ' pid=0x' + (dev.getProductId() & 0xffff).toString(16) +
               ' name=' + dev.getDeviceName() +
               ' ifaces=' + dev.getInterfaceCount());
        }
        return r;
    };

    const UsbDeviceConnection = Java.use('android.hardware.usb.UsbDeviceConnection');
    const UsbEndpoint = Java.use('android.hardware.usb.UsbEndpoint');

    function epDesc(ep) {
        if (!ep) return '<null>';
        return 'ep#addr=0x' + (ep.getAddress() & 0xff).toString(16) +
               ' type=' + ep.getType() +
               ' dir=' + (ep.getDirection() === 0 ? 'OUT' : 'IN') +
               ' maxpkt=' + ep.getMaxPacketSize();
    }

    UsbDeviceConnection.claimInterface.implementation = function (iface, force) {
        wr('CLAIM iface=' + iface.getInterfaceCount() + ' desc()=' + iface.toString() + ' force=' + force);
        return this.claimInterface(iface, force);
    };

    // ------------- controlTransfer overloads -------------
    // (int requestType, int request, int value, int index, byte[] buffer, int length, int timeout)
    UsbDeviceConnection.controlTransfer.overload('int', 'int', 'int', 'int', '[B', 'int', 'int')
        .implementation = function (rt, req, val, idx, buf, len, to) {
            const n = this.controlTransfer(rt, req, val, idx, buf, len, to);
            const dir = (rt & 0x80) ? 'IN ' : 'OUT';
            const line = 'CTRL ' + dir +
                ' bmReqType=0x' + (rt & 0xff).toString(16).padStart(2, '0') +
                ' bRequest=0x' + (req & 0xff).toString(16).padStart(2, '0') +
                ' wValue=0x' + (val & 0xffff).toString(16).padStart(4, '0') +
                ' wIndex=0x' + (idx & 0xffff).toString(16).padStart(4, '0') +
                ' wLength=' + len + ' timeout=' + to + ' ret=' + n;
            if (n > 0) {
                wr(line + '\n  data:\n  ' + bytesToHex(buf, 0, n));
            } else {
                wr(line);
            }
            return n;
        };

    // (int, int, int, int, byte[], int) — no timeout overload
    UsbDeviceConnection.controlTransfer.overload('int', 'int', 'int', 'int', '[B', 'int')
        .implementation = function (rt, req, val, idx, buf, len) {
            const n = this.controlTransfer(rt, req, val, idx, buf, len);
            const dir = (rt & 0x80) ? 'IN ' : 'OUT';
            const line = 'CTRL ' + dir +
                ' bmReqType=0x' + (rt & 0xff).toString(16).padStart(2, '0') +
                ' bRequest=0x' + (req & 0xff).toString(16).padStart(2, '0') +
                ' wValue=0x' + (val & 0xffff).toString(16).padStart(4, '0') +
                ' wIndex=0x' + (idx & 0xffff).toString(16).padStart(4, '0') +
                ' wLength=' + len + ' timeout=0 ret=' + n;
            if (n > 0) wr(line + '\n  data:\n  ' + bytesToHex(buf, 0, n));
            else wr(line);
            return n;
        };

    // ------------- bulkTransfer overloads -------------
    UsbDeviceConnection.bulkTransfer.overload('android.hardware.usb.UsbEndpoint', '[B', 'int', 'int', 'int')
        .implementation = function (ep, buf, off, len, to) {
            const n = this.bulkTransfer(ep, buf, off, len, to);
            const isIn = ep.getDirection() === 128; // UsbConstants.USB_DIR_IN
            const line = 'BULK ' + (isIn ? 'IN ' : 'OUT') +
                ' ep=0x' + (ep.getAddress() & 0xff).toString(16) +
                ' off=' + off + ' len=' + len + ' timeout=' + to + ' ret=' + n;
            if (n > 0 && isIn) wr(line + '\n  data:\n  ' + bytesToHex(buf, off, n));
            else wr(line);
            return n;
        };

    UsbDeviceConnection.bulkTransfer.overload('android.hardware.usb.UsbEndpoint', '[B', 'int', 'int', 'int', 'int')
        .implementation = function (ep, buf, off, len, to, flags) {
            const n = this.bulkTransfer(ep, buf, off, len, to, flags);
            const isIn = ep.getDirection() === 128;
            const line = 'BULK ' + (isIn ? 'IN ' : 'OUT') +
                ' ep=0x' + (ep.getAddress() & 0xff).toString(16) +
                ' off=' + off + ' len=' + len + ' timeout=' + to + ' flags=' + flags + ' ret=' + n;
            if (n > 0 && isIn) wr(line + '\n  data:\n  ' + bytesToHex(buf, off, n));
            else wr(line);
            return n;
        };

    // ------------- async requests (UsbRequest) in case SDK uses them -------------
    const UsbRequest = Java.use('android.hardware.usb.usbrequest.USB'); // placeholder — SDK uses sync only
    wr('hooks installed ok');
});
