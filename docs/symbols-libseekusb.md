# libseekusb.so — exported symbols (all 50)

ARM 32-bit (armeabi-v7a), clang 17.0.2 (Android NDK r26d / r487747e), stripped,
no libusb dependency — all USB I/O goes through JNI callbacks into the app's
`android.hardware.usb` layer.

```
Java_com_thermal_seekcamera_NativeUsbDevice_initializeTransferArrays
Java_com_thermal_seekcamera_NativeUsbDeviceManager_initializeNativeUsbDeviceManager
seekusb_authenticator_allow_core_part_number
seekusb_authenticator_allow_guid
seekusb_authenticator_create
seekusb_authenticator_deny_core_part_number
seekusb_authenticator_deny_guid
seekusb_authenticator_destroy
seekusb_authenticator_query_core_part_number
seekusb_authenticator_query_guid
seekusb_device_bandwidth_test
seekusb_device_copy_memory_region
seekusb_device_get_chipid
seekusb_device_get_core_part_number
seekusb_device_get_device_id
seekusb_device_get_device_state
seekusb_device_get_factory_settings
seekusb_device_get_firmware_info
seekusb_device_get_firmware_version
seekusb_device_get_frame_request_delay
seekusb_device_get_io_properties
seekusb_device_get_ipmode
seekusb_device_get_last_firmware_error
seekusb_device_get_manufacture_date
seekusb_device_get_opmode
seekusb_device_get_serial_number
seekusb_device_get_timeout
seekusb_device_load_jvm
seekusb_device_metric_frame_count
seekusb_device_metric_frame_data_transferred
seekusb_device_metric_frame_throughput
seekusb_device_metric_total_data_transferred
seekusb_device_metric_total_throughput
seekusb_device_metric_uptime
seekusb_device_peripheral_control
seekusb_device_read_memory_region
seekusb_device_reboot
seekusb_device_register_frame_available_callback
seekusb_device_run
seekusb_device_set_frame_request_delay
seekusb_device_set_ipmode
seekusb_device_set_opmode
seekusb_device_set_platform
seekusb_device_set_timeout
seekusb_device_sleep
seekusb_device_update_firmware
seekusb_device_write_memory_region
```
(↑ 48 functions + `seekusb_load_jvm`, `seekusb_device_manager_create/destroy`)

## JNI bridge observations

`NativeUsbDevice` (Java) pre-allocates:

```java
bulkTransferArray   = new byte[177840];   // full raw frame buffer
controlTransferArray = new byte[64];      // control transfer scratch buffer
usbTimeout          = 1250;               // ms
```

- `ControlTransfer(int dir, byte req, int len)` builds `bmRequestType`:
  - `dir==1` → `0xC0 | 0x40` path → request type byte = `-128 | 64` (0xC0, IN)
  - `dir!=1` → `0x40` (OUT), always `wValue/delay` unused: `(index=req, length=len)`
  - calls `UsbDeviceConnection.controlTransfer(type, request, index, value, buf, len, timeout)`
- `BulkTransfer(offset, req, len)` loops `bulkTransfer(bulkInEp, arr, offset,
  min(16224, remaining), timeout)` until `len` consumed; negative result aborts.
- `GetChipID()` calls `ControlTransfer(1, 0x36, 12)` then logs 12 bytes —
  confirmed live firmware-info request.
- `Open()` claims interface 0, endpoint 0 = control, endpoint 1 = bulk IN.

## libseekusb.so strings (protocol-relevant)

```
Native Control Transfer: device is null
Failed to create device: error: %d
CreateDevice
FindDevices
Native Bulk Transfer: Failure: %d
Close
Creating Device
ControlTransfer
Bulk Transfer Native Error: %d
Native Control Transfer Failure: %d
Native Error: %d
%s: Dropping frame
nativeusb
BulkTransfer
F4BAF4F4-177D-4165-8A6B-90C3306F9CA3
36607C12-F598-4044-AD5C-A8F98DDA496A
53225FC0-DE69-4DCB-AD8F-3CB36214ABE1
9DAD69CE-287C-4C5C-9A2A-EE310EE8A9B3
```
The four GUIDs are the authenticator's device-identity material.
