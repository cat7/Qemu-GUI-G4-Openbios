/*
 * winusb-switch: put one USB device on Windows' inbox WinUSB driver
 * (%WINDIR%\INF\winusb.inf, class USBDevice) and give it back.
 *
 *   winusb-switch list [--out FILE]
 *   winusb-switch bind VVVV:PPPP [--out FILE]      (administrator)
 *   winusb-switch unbind VVVV:PPPP [--out FILE]    (administrator)
 *   winusb-switch cleanup-cert [--out FILE]        (administrator)
 *   winusb-switch log
 *
 * bind, unbind and cleanup-cert append their result to
 * %ProgramData%\winusb-switch\log.jsonl.
 *
 * stdout: one JSON object per line; --out writes the same lines to FILE
 * (an elevated run's stdout cannot be read by the program that started it).
 * Exit: 0 done, 3010 done but the device must be replugged (or Windows
 * restarted) to finish, 1 failed, 2 usage, 3 refused, 4 not elevated,
 * 5 in use (a program holds the device; nothing changed).
 *
 * Build (64-bit only; 32-bit on 64-bit Windows cannot install drivers):
 *   x86_64-w64-mingw32-gcc -O2 -Wall -municode -o winusb-switch.exe \
 *       winusb-switch.c -lsetupapi -lcfgmgr32 -lcrypt32 -lwintrust
 */

#ifndef UNICODE
#define UNICODE
#endif
#ifndef _UNICODE
#define _UNICODE
#endif
#undef _WIN32_WINNT
#define _WIN32_WINNT 0x0A00

#include <windows.h>
#include <setupapi.h>
#include <cfgmgr32.h>
#include <winioctl.h>
#include <usbioctl.h>
#include <wincrypt.h>
#include <wintrust.h>
#include <mscat.h>
#include <fcntl.h>
#include <io.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>

#define EXIT_DONE      0
#define EXIT_FAILED    1
#define EXIT_USAGE     2
#define EXIT_REFUSED   3
#define EXIT_NOTADMIN  4
#define EXIT_INUSE     5
#define EXIT_REBOOT    3010

/* newdev.dll, resolved at run time */
#define DIIDFLAG_INSTALLNULLDRIVER_ 0x00000004
typedef BOOL (WINAPI *DiInstallDevice_fn)(HWND, HDEVINFO, PSP_DEVINFO_DATA,
                                          PSP_DRVINFO_DATA_W, DWORD, PBOOL);
typedef BOOL (WINAPI *DiUninstallDevice_fn)(HWND, HDEVINFO, PSP_DEVINFO_DATA,
                                            DWORD, PBOOL);

/* Setup classes */
#define CLS_USBDEVICE L"{88bae032-5a81-49f0-bc3d-a4ff138216d6}"
#define CLS_KEYBOARD  L"{4d36e96b-e325-11ce-bfc1-08002be10318}"
#define CLS_MOUSE     L"{4d36e96f-e325-11ce-bfc1-08002be10318}"
#define CLS_DISK      L"{4d36e967-e325-11ce-bfc1-08002be10318}"
#define CLS_BLUETOOTH L"{e0cbf06c-cd8b-4647-bb8a-263b43f0f974}"
#define CLS_USB       L"{36fc9e60-c465-11cf-8056-444553540000}"

/* Provider of the own packages (see inf_bind) */
#define PROVIDER      L"winusb-switch"

/* Class GUID before bind, in the device's hardware key */
#define SAVED_CLASS   L"WinusbSwitchClassGUID"

#define PENDING_MSG "replug the device (or restart Windows) to finish"

/* DEVPKEY_Device_DriverDesc, _DriverInfPath, _BusReportedDeviceDesc */
static const DEVPROPKEY PK_DRIVERDESC = {
    { 0xa8b865dd, 0x2e3d, 0x4094,
      { 0xad, 0x97, 0xe5, 0x93, 0xa7, 0x0c, 0x75, 0xd6 } }, 4 };
static const DEVPROPKEY PK_INFPATH = {
    { 0xa8b865dd, 0x2e3d, 0x4094,
      { 0xad, 0x97, 0xe5, 0x93, 0xa7, 0x0c, 0x75, 0xd6 } }, 5 };
static const DEVPROPKEY PK_PROVIDER = {
    { 0xa8b865dd, 0x2e3d, 0x4094,
      { 0xad, 0x97, 0xe5, 0x93, 0xa7, 0x0c, 0x75, 0xd6 } }, 9 };
static const DEVPROPKEY PK_BUSDESC = {
    { 0x540b947e, 0x8b40, 0x45bc,
      { 0xa8, 0xa2, 0x6a, 0x0b, 0x89, 0x4c, 0xbd, 0xa2 } }, 4 };

#define WINUSB_HWID L"USB\\MS_COMP_WINUSB"

/* GUID_DEVINTERFACE_USB_HUB */
static const GUID HUB_INTERFACE = {
    0xf18a0e88, 0xc30c, 0x11d0,
    { 0x88, 0x15, 0x00, 0xa0, 0xc9, 0x06, 0xbe, 0xd8 } };

/* ---- output ------------------------------------------------------------ */

static FILE *out_file;

typedef struct {
    char *p;
    size_t n, cap;
} Buf;

static void oom(void)
{
    static const char msg[] = "{\"ok\":false,\"error\":\"out of memory\"}\n";
    fputs(msg, stdout);
    if (out_file) {
        fputs(msg, out_file);
        fclose(out_file);
    }
    exit(EXIT_FAILED);
}

static void b_put(Buf *b, const char *s, size_t len)
{
    if (b->n + len + 1 > b->cap) {
        size_t c = b->cap ? b->cap : 256;
        char *q;
        while (c < b->n + len + 1) {
            c *= 2;
        }
        q = realloc(b->p, c);
        if (!q) {
            oom();
        }
        b->p = q;
        b->cap = c;
    }
    memcpy(b->p + b->n, s, len);
    b->n += len;
    b->p[b->n] = 0;
}

static void b_str(Buf *b, const char *s)
{
    b_put(b, s, strlen(s));
}

static void b_jstr(Buf *b, const char *s)
{
    char t[8];

    b_put(b, "\"", 1);
    for (; *s; s++) {
        unsigned char c = (unsigned char)*s;
        if (c == '"' || c == '\\') {
            t[0] = '\\';
            t[1] = (char)c;
            b_put(b, t, 2);
        } else if (c < 0x20) {
            snprintf(t, sizeof t, "\\u%04x", c);
            b_put(b, t, 6);
        } else {
            b_put(b, (const char *)&c, 1);
        }
    }
    b_put(b, "\"", 1);
}

static char *utf8(const wchar_t *w)
{
    int n;
    char *s;

    if (!w) {
        w = L"";
    }
    n = WideCharToMultiByte(CP_UTF8, 0, w, -1, NULL, 0, NULL, NULL);
    if (n <= 0) {
        n = 1;
        w = L"";
    }
    s = malloc((size_t)n);
    if (!s) {
        oom();
    }
    if (WideCharToMultiByte(CP_UTF8, 0, w, -1, s, n, NULL, NULL) <= 0) {
        s[0] = 0;
    }
    return s;
}

/* One JSON object */
typedef struct {
    Buf b;
    int fields;
} J;

static void j_open(J *j)
{
    memset(j, 0, sizeof *j);
    b_str(&j->b, "{");
}

static void j_key(J *j, const char *k)
{
    if (j->fields++) {
        b_str(&j->b, ",");
    }
    b_jstr(&j->b, k);
    b_str(&j->b, ":");
}

static void j_s(J *j, const char *k, const char *v)
{
    j_key(j, k);
    b_jstr(&j->b, v ? v : "");
}

static void j_w(J *j, const char *k, const wchar_t *v)
{
    char *s = utf8(v);
    j_s(j, k, s);
    free(s);
}

static void j_i(J *j, const char *k, long long v)
{
    char t[32];
    snprintf(t, sizeof t, "%lld", v);
    j_key(j, k);
    b_str(&j->b, t);
}

static void j_b(J *j, const char *k, int v)
{
    j_key(j, k);
    b_str(&j->b, v ? "true" : "false");
}

static void j_raw(J *j, const char *k, const char *json)
{
    j_key(j, k);
    b_str(&j->b, json);
}

static void j_id(J *j, unsigned vid, unsigned pid)
{
    char t[16];
    snprintf(t, sizeof t, "%04x:%04x", vid & 0xffff, pid & 0xffff);
    j_s(j, "id", t);
}

/* Close into a string owned by the caller. */
static char *j_take(J *j)
{
    b_str(&j->b, "}");
    return j->b.p;
}

/* "code" and "message" of a Win32, SetupAPI or HRESULT error into e */
static void j_code(J *e, DWORD code)
{
    wchar_t *msg = NULL;
    DWORD fm = code;
    char t[16];

    snprintf(t, sizeof t, "0x%08lx", (unsigned long)code);
    j_s(e, "code", t);
    /* SetupAPI errors (0xE000xxxx) have text under their HRESULT form */
    if ((code & 0xE0000000u) == 0xE0000000u) {
        fm = (code & 0xFFFFu) | (15u << 16) | 0x80000000u;
    }
    if (FormatMessageW(FORMAT_MESSAGE_ALLOCATE_BUFFER |
                       FORMAT_MESSAGE_FROM_SYSTEM |
                       FORMAT_MESSAGE_IGNORE_INSERTS,
                       NULL, fm, 0, (LPWSTR)&msg, 0, NULL) && msg) {
        size_t n = wcslen(msg);
        while (n && (msg[n - 1] == L'\r' || msg[n - 1] == L'\n' ||
                     msg[n - 1] == L' ' || msg[n - 1] == L'.')) {
            msg[--n] = 0;
        }
        j_w(e, "message", msg);
        LocalFree(msg);
    }
}

/* key: {"code":"0x...","message":"..."} */
static void j_err(J *j, const char *key, DWORD code)
{
    J e;
    char *s;

    j_open(&e);
    j_code(&e, code);
    s = j_take(&e);
    j_raw(j, key, s);
    free(s);
}

/* ---- steps: every call that changes something, for the result -------- */

static Buf steps;
static int nsteps;

static void step_add(const char *call, int ok, const char *ret, DWORD err,
                     const char *note)
{
    J e;
    char *s;

    j_open(&e);
    j_s(&e, "call", call);
    j_b(&e, "ok", ok);
    if (ret) {
        j_s(&e, "ret", ret);
    }
    if (!ok && err) {
        j_code(&e, err);
    }
    if (note) {
        j_s(&e, "note", note);
    }
    s = j_take(&e);
    b_str(&steps, nsteps++ ? "," : "");
    b_str(&steps, s);
    free(s);
}

/* BOOL call: logged with GetLastError on failure, which is kept */
static BOOL sb(const char *call, BOOL r)
{
    DWORD e = r ? 0 : GetLastError();
    step_add(call, r != 0, r ? "TRUE" : "FALSE", e, NULL);
    SetLastError(e);
    return r;
}

static CONFIGRET scr(const char *call, CONFIGRET cr)
{
    char t[24];
    snprintf(t, sizeof t, "CR 0x%02lx", (unsigned long)cr);
    step_add(call, cr == CR_SUCCESS, t, 0, NULL);
    return cr;
}

static HRESULT shr(const char *call, HRESULT hr)
{
    char t[16];
    snprintf(t, sizeof t, "0x%08lx", (unsigned long)hr);
    step_add(call, hr == S_OK, t, (DWORD)hr, NULL);
    return hr;
}

static void snote(const char *call, const char *note)
{
    step_add(call, 1, NULL, 0, note);
}

/* 1 when the devnode exists, is started and has no problem */
static int dn_started(DEVINST dn)
{
    ULONG st = 0, pr = 0;
    return CM_Get_DevNode_Status(&st, &pr, dn, 0) == CR_SUCCESS &&
           (st & DN_STARTED) && !(st & DN_HAS_PROBLEM);
}

/* The devnode's status into the steps */
static void sstate(const char *label, DEVINST dn)
{
    ULONG st = 0, pr = 0;
    char t[64];

    if (CM_Get_DevNode_Status(&st, &pr, dn, 0) != CR_SUCCESS) {
        snote(label, "no devnode");
        return;
    }
    snprintf(t, sizeof t, "status 0x%08lx problem %lu%s", (unsigned long)st,
             (unsigned long)((st & DN_HAS_PROBLEM) ? pr : 0),
             (st & DN_STARTED) ? " started" : "");
    snote(label, t);
}

/* Close, print one line, free. */
static int log_results;
static void log_append(const char *line, size_t len);

static void j_print(J *j)
{
    b_str(&j->b, "}\n");
    if (log_results) {
        log_append(j->b.p, j->b.n);
    }
    fwrite(j->b.p, 1, j->b.n, stdout);
    if (out_file) {
        fwrite(j->b.p, 1, j->b.n, out_file);
        fflush(out_file);
    }
    fflush(stdout);
    free(j->b.p);
    j->b.p = NULL;
}

/* ---- strings ----------------------------------------------------------- */

static const wchar_t *wcsistr(const wchar_t *h, const wchar_t *n)
{
    size_t k = wcslen(n);
    for (; *h; h++) {
        if (_wcsnicmp(h, n, k) == 0) {
            return h;
        }
    }
    return NULL;
}

static int msz_has_prefix(const wchar_t *m, const wchar_t *prefix)
{
    size_t k = wcslen(prefix);
    if (!m) {
        return 0;
    }
    for (; *m; m += wcslen(m) + 1) {
        if (_wcsnicmp(m, prefix, k) == 0) {
            return 1;
        }
    }
    return 0;
}

static int hex4(const wchar_t *s, unsigned *v)
{
    unsigned r = 0;
    int i;

    for (i = 0; i < 4; i++) {
        wchar_t c = s[i];
        r <<= 4;
        if (c >= L'0' && c <= L'9') {
            r |= (unsigned)(c - L'0');
        } else if (c >= L'a' && c <= L'f') {
            r |= (unsigned)(c - L'a' + 10);
        } else if (c >= L'A' && c <= L'F') {
            r |= (unsigned)(c - L'A' + 10);
        } else {
            return 0;
        }
    }
    *v = r;
    return 1;
}

/*
 * 1 when the hardware IDs name a whole USB device: USB\VID_v&PID_p with an
 * optional &REV_r, and no &MI_xx (a function of a composite device).
 */
static int usb_ids(const wchar_t *hwids, unsigned *vid, unsigned *pid)
{
    const wchar_t *s;

    if (!hwids) {
        return 0;
    }
    for (s = hwids; *s; s += wcslen(s) + 1) {
        if (wcsistr(s, L"&MI_")) {
            return 0;
        }
    }
    for (s = hwids; *s; s += wcslen(s) + 1) {
        if (_wcsnicmp(s, L"USB\\VID_", 8) != 0 || !hex4(s + 8, vid) ||
            _wcsnicmp(s + 12, L"&PID_", 5) != 0 || !hex4(s + 17, pid)) {
            continue;
        }
        if (s[21] == 0 || _wcsnicmp(s + 21, L"&REV_", 5) == 0) {
            return 1;
        }
    }
    return 0;
}

static int parse_id(const wchar_t *s, unsigned *vid, unsigned *pid)
{
    return wcslen(s) == 9 && hex4(s, vid) && s[4] == L':' &&
           hex4(s + 5, pid);
}

static int is_winusb(const wchar_t *service)
{
    return _wcsicmp(service, L"WinUSB") == 0;
}

/* ---- device properties ------------------------------------------------- */

/* REG_SZ / REG_MULTI_SZ SetupAPI property, double-NUL terminated; free() */
static wchar_t *dev_prop(HDEVINFO h, SP_DEVINFO_DATA *d, DWORD prop)
{
    DWORD type = 0, need = 0;
    BYTE *buf;

    if (!SetupDiGetDeviceRegistryPropertyW(h, d, prop, &type, NULL, 0, &need)
        && GetLastError() != ERROR_INSUFFICIENT_BUFFER) {
        return NULL;
    }
    if (need == 0) {
        return NULL;
    }
    buf = calloc(need + 2 * sizeof(wchar_t), 1);
    if (!buf) {
        oom();
    }
    if (!SetupDiGetDeviceRegistryPropertyW(h, d, prop, &type, buf, need,
                                           NULL) ||
        (type != REG_SZ && type != REG_MULTI_SZ && type != REG_EXPAND_SZ)) {
        free(buf);
        return NULL;
    }
    return (wchar_t *)buf;
}

static void dev_prop_into(HDEVINFO h, SP_DEVINFO_DATA *d, DWORD prop,
                          wchar_t *out, size_t cch)
{
    wchar_t *v = dev_prop(h, d, prop);
    out[0] = 0;
    if (v) {
        wcsncpy(out, v, cch - 1);
        out[cch - 1] = 0;
        free(v);
    }
}

static void dev_pkey_into(HDEVINFO h, SP_DEVINFO_DATA *d, const DEVPROPKEY *k,
                          wchar_t *out, size_t cch)
{
    DEVPROPTYPE t = 0;

    if (!SetupDiGetDevicePropertyW(h, d, k, &t, (PBYTE)out,
                                   (DWORD)(cch * sizeof(wchar_t)), NULL, 0) ||
        t != DEVPROP_TYPE_STRING) {
        out[0] = 0;
    }
    out[cch - 1] = 0;
}

/* Same as dev_prop, by devnode; free() */
static wchar_t *cm_prop(DEVINST dn, ULONG prop)
{
    ULONG type = 0, len = 0;
    BYTE *buf;

    if (CM_Get_DevNode_Registry_PropertyW(dn, prop, &type, NULL, &len, 0) !=
        CR_BUFFER_SMALL || len == 0) {
        return NULL;
    }
    buf = calloc(len + 2 * sizeof(wchar_t), 1);
    if (!buf) {
        oom();
    }
    if (CM_Get_DevNode_Registry_PropertyW(dn, prop, &type, buf, &len, 0) !=
        CR_SUCCESS) {
        free(buf);
        return NULL;
    }
    return (wchar_t *)buf;
}

static int dn_present(DEVINST dn, ULONG *problem)
{
    ULONG st = 0, pr = 0;
    if (CM_Get_DevNode_Status(&st, &pr, dn, 0) != CR_SUCCESS) {
        if (problem) {
            *problem = 0;
        }
        return 0;
    }
    if (problem) {
        *problem = (st & DN_HAS_PROBLEM) ? pr : 0;
    }
    return 1;
}

/* Device info set holding one device; destroy with SetupDiDestroyDeviceInfoList */
static HDEVINFO open_dev(const wchar_t *inst, SP_DEVINFO_DATA *d)
{
    HDEVINFO h = SetupDiCreateDeviceInfoList(NULL, NULL);
    if (h == INVALID_HANDLE_VALUE) {
        return h;
    }
    d->cbSize = sizeof *d;
    if (!SetupDiOpenDeviceInfoW(h, inst, NULL, 0, d)) {
        DWORD e = GetLastError();
        SetupDiDestroyDeviceInfoList(h);
        SetLastError(e);
        return INVALID_HANDLE_VALUE;
    }
    return h;
}

typedef struct {
    wchar_t inst[MAX_DEVICE_ID_LEN];
    DEVINST dn;
    unsigned vid, pid;
    int present;
} Dev;

/* USB devices (not functions); present only, or also absent ones; free() */
static int enum_usb(int present_only, Dev **out)
{
    HDEVINFO h;
    SP_DEVINFO_DATA d;
    Dev *v = NULL;
    int n = 0, cap = 0;
    DWORD i;

    *out = NULL;
    h = SetupDiGetClassDevsW(NULL, L"USB", NULL,
                             DIGCF_ALLCLASSES |
                             (present_only ? DIGCF_PRESENT : 0));
    if (h == INVALID_HANDLE_VALUE) {
        return -1;
    }
    d.cbSize = sizeof d;
    for (i = 0; SetupDiEnumDeviceInfo(h, i, &d); i++) {
        wchar_t *hw = dev_prop(h, &d, SPDRP_HARDWAREID);
        unsigned vid = 0, pid = 0;
        int ok = usb_ids(hw, &vid, &pid);
        Dev *e;

        free(hw);
        if (!ok) {
            continue;
        }
        if (n == cap) {
            Dev *q;
            cap = cap ? cap * 2 : 16;
            q = realloc(v, (size_t)cap * sizeof *v);
            if (!q) {
                oom();
            }
            v = q;
        }
        e = &v[n];
        memset(e, 0, sizeof *e);
        if (!SetupDiGetDeviceInstanceIdW(h, &d, e->inst, MAX_DEVICE_ID_LEN,
                                         NULL)) {
            continue;
        }
        e->dn = d.DevInst;
        e->vid = vid;
        e->pid = pid;
        e->present = dn_present(d.DevInst, NULL);
        n++;
    }
    SetupDiDestroyDeviceInfoList(h);
    *out = v;
    return n;
}

/*
 * The speed the device runs at, from its port on the parent hub
 * (IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX, and _V2 for SuperSpeed):
 * "low", "full", "high", "super", or "" when unknown.
 */
static const char *usb_speed(HDEVINFO h, SP_DEVINFO_DATA *d)
{
    union {
        USB_NODE_CONNECTION_INFORMATION_EX ci;
        BYTE raw[sizeof(USB_NODE_CONNECTION_INFORMATION_EX) +
                 32 * sizeof(USB_PIPE_INFO)];
    } u;
    USB_NODE_CONNECTION_INFORMATION_EX_V2 v2;
    wchar_t hub_id[MAX_DEVICE_ID_LEN], *ifs;
    DEVINST parent;
    DWORD port = 0, type = 0, got = 0;
    ULONG len = 0;
    HANDLE hub;
    const char *r = "";

    if (!SetupDiGetDeviceRegistryPropertyW(h, d, SPDRP_ADDRESS, &type,
                                           (PBYTE)&port, sizeof port, NULL) ||
        type != REG_DWORD || port == 0) {
        return "";
    }
    if (CM_Get_Parent(&parent, d->DevInst, 0) != CR_SUCCESS ||
        CM_Get_Device_IDW(parent, hub_id, MAX_DEVICE_ID_LEN, 0) !=
        CR_SUCCESS) {
        return "";
    }
    if (CM_Get_Device_Interface_List_SizeW(&len, (LPGUID)&HUB_INTERFACE,
                                           hub_id,
                                           CM_GET_DEVICE_INTERFACE_LIST_PRESENT)
        != CR_SUCCESS || len < 2) {
        return "";
    }
    ifs = calloc(len + 1, sizeof(wchar_t));
    if (!ifs) {
        oom();
    }
    if (CM_Get_Device_Interface_ListW((LPGUID)&HUB_INTERFACE, hub_id, ifs,
                                      len,
                                      CM_GET_DEVICE_INTERFACE_LIST_PRESENT) !=
        CR_SUCCESS || !ifs[0]) {
        free(ifs);
        return "";
    }
    hub = CreateFileW(ifs, GENERIC_WRITE, FILE_SHARE_WRITE, NULL,
                      OPEN_EXISTING, 0, NULL);
    free(ifs);
    if (hub == INVALID_HANDLE_VALUE) {
        return "";
    }
    memset(&u, 0, sizeof u);
    u.ci.ConnectionIndex = port;
    if (DeviceIoControl(hub, IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX,
                        &u, sizeof u, &u, sizeof u, &got, NULL) &&
        u.ci.ConnectionStatus == DeviceConnected) {
        switch (u.ci.Speed) {
        case UsbLowSpeed:
            r = "low";
            break;
        case UsbFullSpeed:
            r = "full";
            break;
        case UsbHighSpeed:
            r = "high";
            break;
        case UsbSuperSpeed:
            r = "super";
            break;
        }
        /* _EX reports at most high speed */
        memset(&v2, 0, sizeof v2);
        v2.ConnectionIndex = port;
        v2.Length = sizeof v2;
        v2.SupportedUsbProtocols.ul = 0x4;        /* Usb300 */
        if (u.ci.Speed == UsbHighSpeed &&
            DeviceIoControl(hub,
                            IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX_V2,
                            &v2, sizeof v2, &v2, sizeof v2, &got, NULL) &&
            (v2.Flags.ul & 0x5)) {   /* operating at SuperSpeed or SS+ */
            r = "super";
        }
    }
    CloseHandle(hub);
    return r;
}

typedef struct {
    wchar_t desc[256], product[256], drvdesc[256];
    wchar_t cls[64], clsguid[64], service[64], inf[MAX_PATH], provider[64];
    int composite, present, started;
    const char *speed;
    ULONG problem;
} Info;

static int get_info(const wchar_t *inst, Info *in)
{
    SP_DEVINFO_DATA d;
    HDEVINFO h;
    wchar_t *compat;

    memset(in, 0, sizeof *in);
    h = open_dev(inst, &d);
    if (h == INVALID_HANDLE_VALUE) {
        return 0;
    }
    dev_prop_into(h, &d, SPDRP_FRIENDLYNAME, in->desc, 256);
    if (!in->desc[0]) {
        dev_prop_into(h, &d, SPDRP_DEVICEDESC, in->desc, 256);
    }
    dev_pkey_into(h, &d, &PK_BUSDESC, in->product, 256);
    dev_pkey_into(h, &d, &PK_DRIVERDESC, in->drvdesc, 256);
    dev_pkey_into(h, &d, &PK_INFPATH, in->inf, MAX_PATH);
    dev_pkey_into(h, &d, &PK_PROVIDER, in->provider, 64);
    dev_prop_into(h, &d, SPDRP_CLASS, in->cls, 64);
    dev_prop_into(h, &d, SPDRP_CLASSGUID, in->clsguid, 64);
    dev_prop_into(h, &d, SPDRP_SERVICE, in->service, 64);
    compat = dev_prop(h, &d, SPDRP_COMPATIBLEIDS);
    in->composite = msz_has_prefix(compat, L"USB\\COMPOSITE") &&
                    !_wcsicmp(in->service, L"usbccgp");
    free(compat);
    in->present = dn_present(d.DevInst, &in->problem);
    in->started = dn_started(d.DevInst);
    in->speed = in->present ? usb_speed(h, &d) : "";
    SetupDiDestroyDeviceInfoList(h);
    return 1;
}

static void j_info(J *j, const Info *in)
{
    j_w(j, "description", in->desc);
    j_w(j, "product", in->product);
    j_w(j, "class", in->cls);
    j_w(j, "class_guid", in->clsguid);
    j_w(j, "service", in->service);
    j_w(j, "inf", in->inf);
    j_w(j, "driver", in->drvdesc);
    j_w(j, "provider", in->provider);
    j_s(j, "speed", in->speed);
    j_b(j, "composite", in->composite);
    j_b(j, "winusb", is_winusb(in->service));
    j_i(j, "problem", (long long)in->problem);
    j_b(j, "started", in->started);
}

/* ---- what may never be switched ---------------------------------------- */

typedef struct {
    const char *refuse;
    Buf funcs;
    int nfuncs;
    int self_hid, ifaces, hid_ifaces;   /* interface functions, HID ones */
} Walk;

static void refuse(Walk *w, const char *why)
{
    if (!w->refuse) {
        w->refuse = why;
    }
}

/* The node and everything below it: keyboards, mice, hubs, Bluetooth, disks */
static void walk(DEVINST dn, int depth, Walk *w)
{
    wchar_t *guid, *svc, *compat;
    DEVINST child, next;

    int iface = 0;

    if (depth > 0) {
        /* A whole USB device below a hub is not a function of this one */
        wchar_t *hw = cm_prop(dn, CM_DRP_HARDWAREID);
        unsigned v, p;
        int other = usb_ids(hw, &v, &p);
        const wchar_t *s;
        for (s = hw; s && *s && !iface; s += wcslen(s) + 1) {
            iface = wcsistr(s, L"&MI_") != NULL;
        }
        free(hw);
        if (other) {
            return;
        }
    }
    guid = cm_prop(dn, CM_DRP_CLASSGUID);
    svc = cm_prop(dn, CM_DRP_SERVICE);
    compat = cm_prop(dn, CM_DRP_COMPATIBLEIDS);

    /* A single-interface HID device, or a composite one's HID functions */
    if (depth == 0 && msz_has_prefix(compat, L"USB\\Class_03")) {
        w->self_hid = 1;
    }
    if (depth == 1 && iface) {
        w->ifaces++;
        if (msz_has_prefix(compat, L"USB\\Class_03")) {
            w->hid_ifaces++;
        }
    }

    if (guid) {
        if (!_wcsicmp(guid, CLS_KEYBOARD) || !_wcsicmp(guid, CLS_MOUSE)) {
            refuse(w, "keyboard or mouse");
        } else if (!_wcsicmp(guid, CLS_BLUETOOTH)) {
            refuse(w, "bluetooth");
        } else if (!_wcsicmp(guid, CLS_DISK)) {
            refuse(w, "disk");
        }
    }
    if (msz_has_prefix(compat, L"USB\\Class_03&SubClass_01&Prot_01") ||
        msz_has_prefix(compat, L"USB\\Class_03&SubClass_01&Prot_02")) {
        refuse(w, "keyboard or mouse");
    }
    if (msz_has_prefix(compat, L"USB\\Class_09") ||
        msz_has_prefix(compat, L"USB\\DevClass_09") ||
        (svc && (!_wcsicmp(svc, L"usbhub") || !_wcsicmp(svc, L"USBHUB3")))) {
        refuse(w, "hub");
    }
    if (msz_has_prefix(compat, L"USB\\Class_E0&SubClass_01&Prot_01")) {
        refuse(w, "bluetooth");
    }
    if (depth == 1) {
        J f;
        wchar_t *cls = cm_prop(dn, CM_DRP_CLASS);
        char *s;

        j_open(&f);
        j_w(&f, "class", cls);
        j_w(&f, "service", svc);
        s = j_take(&f);
        b_str(&w->funcs, w->nfuncs++ ? "," : "");
        b_str(&w->funcs, s);
        free(s);
        free(cls);
    }
    free(guid);
    free(svc);
    free(compat);

    if (depth >= 8) {
        return;
    }
    if (CM_Get_Child(&child, dn, 0) != CR_SUCCESS) {
        return;
    }
    for (;;) {
        walk(child, depth + 1, w);
        if (CM_Get_Sibling(&next, child, 0) != CR_SUCCESS) {
            break;
        }
        child = next;
    }
}

/* Refusal reason or NULL; functions as a JSON array, free() */
static const char *check(DEVINST dn, char **funcs)
{
    Walk w;

    memset(&w, 0, sizeof w);
    b_str(&w.funcs, "[");
    walk(dn, 0, &w);
    b_str(&w.funcs, "]");
    /* LED controllers, macro pads: nothing but HID (a headset's HID stays) */
    if (w.self_hid || (w.ifaces > 0 && w.hid_ifaces == w.ifaces)) {
        refuse(&w, "HID only");
    }
    if (funcs) {
        *funcs = w.funcs.p;
    } else {
        free(w.funcs.p);
    }
    return w.refuse;
}

/* ---- list -------------------------------------------------------------- */

static char *hijack_warnings(const Dev *devs, int n);

static int cmd_list(void)
{
    Dev *v;
    int n = enum_usb(1, &v), i, k;

    if (n < 0) {
        DWORD e = GetLastError();
        J j;
        j_open(&j);
        j_s(&j, "op", "list");
        j_b(&j, "ok", 0);
        j_s(&j, "error", "cannot enumerate USB devices");
        j_err(&j, "detail", e);
        j_print(&j);
        return EXIT_FAILED;
    }
    {
        J j;
        char *w = hijack_warnings(v, n);
        j_open(&j);
        j_s(&j, "op", "list");
        j_raw(&j, "warnings", w);
        j_print(&j);
        free(w);
    }
    for (i = 0; i < n; i++) {
        J j;
        Info in;
        char *funcs = NULL;
        const char *why;
        int same = 0;

        for (k = 0; k < n; k++) {
            if (v[k].vid == v[i].vid && v[k].pid == v[i].pid) {
                same++;
            }
        }
        get_info(v[i].inst, &in);
        why = check(v[i].dn, &funcs);
        if (!why && same > 1) {
            why = "ambiguous";
        }
        j_open(&j);
        j_id(&j, v[i].vid, v[i].pid);
        j_w(&j, "instance", v[i].inst);
        j_info(&j, &in);
        j_raw(&j, "functions", funcs);
        j_s(&j, "refuse", why ? why : "");
        j_print(&j);
        free(funcs);
    }
    free(v);
    return EXIT_DONE;
}

/* ---- shared by bind and unbind ----------------------------------------- */

static int is_elevated(void)
{
    HANDLE tok = NULL;
    TOKEN_ELEVATION e;
    DWORD len = 0;
    int r = 0;

    if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &tok)) {
        return 0;
    }
    if (GetTokenInformation(tok, TokenElevation, &e, sizeof e, &len)) {
        r = e.TokenIsElevated != 0;
    }
    CloseHandle(tok);
    return r;
}

static FARPROC newdev_fn(const char *name)
{
    static HMODULE m;
    if (!m) {
        m = LoadLibraryExW(L"newdev.dll", NULL, LOAD_LIBRARY_SEARCH_SYSTEM32);
    }
    return m ? GetProcAddress(m, name) : NULL;
}

/* bind, unbind and cleanup-cert results go to %ProgramData%\winusb-switch */

static int log_path(wchar_t *out, DWORD cch, int make_dir)
{
    DWORD n = GetEnvironmentVariableW(L"ProgramData", out, cch);
    if (n == 0 || n + 32 >= cch) {
        return 0;
    }
    wcscat(out, L"\\winusb-switch");
    if (make_dir) {
        CreateDirectoryW(out, NULL);
    }
    wcscat(out, L"\\log.jsonl");
    return 1;
}

static void log_append(const char *line, size_t len)
{
    wchar_t path[MAX_PATH], old[MAX_PATH];
    WIN32_FILE_ATTRIBUTE_DATA fa;
    HANDLE f;
    DWORD wrote;

    if (!log_path(path, MAX_PATH, 1)) {
        return;
    }
    /* Keep it small: one older generation */
    if (GetFileAttributesExW(path, GetFileExInfoStandard, &fa) &&
        (fa.nFileSizeHigh || fa.nFileSizeLow > 1024 * 1024)) {
        wcscpy(old, path);
        wcscpy(old + wcslen(old) - 6, L".1.jsonl");
        MoveFileExW(path, old, MOVEFILE_REPLACE_EXISTING);
    }
    f = CreateFileW(path, FILE_APPEND_DATA, FILE_SHARE_READ, NULL,
                    OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (f == INVALID_HANDLE_VALUE) {
        return;
    }
    WriteFile(f, line, (DWORD)len, &wrote, NULL);
    CloseHandle(f);
}

static int finish(J *j, int code)
{
    if (log_results) {
        SYSTEMTIME t;
        char ts[48];
        GetSystemTime(&t);
        snprintf(ts, sizeof ts, "%04u-%02u-%02uT%02u:%02u:%02uZ", t.wYear,
                 t.wMonth, t.wDay, t.wHour, t.wMinute, t.wSecond);
        j_s(j, "time", ts);
        j_i(j, "exit", code);
    }
    if (nsteps) {
        Buf b = { 0 };
        b_str(&b, "[");
        b_str(&b, steps.p);
        b_str(&b, "]");
        j_raw(j, "steps", b.p);
        free(b.p);
    }
    j_print(j);
    return code;
}

static int fail(J *j, const char *msg, DWORD err)
{
    j_b(j, "ok", 0);
    j_s(j, "error", msg);
    if (err) {
        j_err(j, "detail", err);
    }
    return finish(j, EXIT_FAILED);
}

/*
 * The one present device with this id. 0 and a filled Dev, or an exit code
 * after writing the reason into j. Refusals apply.
 */
static int find_target(unsigned vid, unsigned pid, Dev *out, J *j,
                       int refusals)
{
    Dev *v;
    int n = enum_usb(1, &v), i, hits = 0;
    const char *why;
    Buf list = { 0 };

    if (n < 0) {
        return fail(j, "cannot enumerate USB devices", GetLastError());
    }
    b_str(&list, "[");
    for (i = 0; i < n; i++) {
        if (v[i].vid == vid && v[i].pid == pid) {
            char *s = utf8(v[i].inst);
            if (hits++) {
                b_str(&list, ",");
            }
            b_jstr(&list, s);
            free(s);
            *out = v[i];
        }
    }
    b_str(&list, "]");
    free(v);
    if (hits == 0) {
        free(list.p);
        return fail(j, "no such device present", 0);
    }
    if (hits > 1) {
        j_b(j, "ok", 0);
        j_s(j, "refuse", "ambiguous");
        j_raw(j, "instances", list.p);
        free(list.p);
        return finish(j, EXIT_REFUSED);
    }
    free(list.p);
    j_w(j, "instance", out->inst);
    why = refusals ? check(out->dn, NULL) : NULL;
    if (why) {
        j_b(j, "ok", 0);
        j_s(j, "refuse", why);
        return finish(j, EXIT_REFUSED);
    }
    return 0;
}

static void rescan(DEVINST parent)
{
    DEVINST root;

    if (parent) {
        scr("CM_Reenumerate_DevNode(parent)",
            CM_Reenumerate_DevNode(parent, CM_REENUMERATE_SYNCHRONOUS));
    }
    if (CM_Locate_DevNodeW(&root, NULL, CM_LOCATE_DEVNODE_NORMAL) ==
        CR_SUCCESS) {
        scr("CM_Reenumerate_DevNode(root)",
            CM_Reenumerate_DevNode(root, CM_REENUMERATE_SYNCHRONOUS));
    }
}

static const char *veto_text(int t)
{
    static const char *n[] = {
        "unknown", "legacy device", "pending close", "windows app",
        "windows service", "outstanding open", "device", "driver",
        "illegal device request", "insufficient power", "non-disableable",
        "legacy driver", "insufficient rights", "already removed",
    };
    return t >= 0 && t < (int)(sizeof n / sizeof n[0]) ? n[t] : "unknown";
}

/*
 * Stop the device and its functions as "safely remove" does. 0 done;
 * otherwise an exit code after writing the reason into j. A veto (an open
 * handle, usually) changes nothing.
 */
static int quiesce(DEVINST dn, J *j)
{
    PNP_VETO_TYPE vt = PNP_VetoTypeUnknown;
    wchar_t name[MAX_PATH + 2];
    CONFIGRET cr;
    char t[16];

    /* May come back unterminated or as a multi-sz; the first string only */
    memset(name, 0, sizeof name);
    sstate("state before stop", dn);
    cr = scr("CM_Query_And_Remove_SubTreeW",
             CM_Query_And_Remove_SubTreeW(dn, &vt, name, MAX_PATH,
                                          CM_REMOVE_UI_NOT_OK));
    if (cr == CR_SUCCESS) {
        sstate("state after stop", dn);
        return 0;
    }
    j_b(j, "ok", 0);
    if (cr == CR_REMOVE_VETOED) {
        name[MAX_PATH] = 0;
        j_s(j, "refuse", "in use");
        j_i(j, "veto_type", (long long)vt);
        j_s(j, "veto", veto_text((int)vt));
        j_w(j, "veto_name", name);
        j_s(j, "error", "device in use: quit the program using it first "
                        "(QEMU, Camera app, ...)");
        return finish(j, EXIT_INUSE);
    }
    snprintf(t, sizeof t, "0x%02lx", (unsigned long)cr);
    j_s(j, "error", "cannot stop device");
    j_s(j, "cr", t);
    return finish(j, EXIT_FAILED);
}

/*
 * Start a quiesced device again; 1 once it runs without a problem. A
 * storage device stopped this way is held for eject (problem 47) and does
 * not start until it is removed and enumerated afresh.
 */
static int restart(DEVINST dn)
{
    DEVINST parent = 0;
    int t;

    CM_Get_Parent(&parent, dn, 0);
    scr("CM_Setup_DevNode(READY)", CM_Setup_DevNode(dn, CM_SETUP_DEVNODE_READY));
    for (t = 0; t < 40; t++) {
        if (dn_started(dn)) {
            sstate("state after restart", dn);
            return 1;
        }
        if (t == 20) {
            rescan(parent);
        }
        Sleep(250);
    }
    sstate("state after restart", dn);
    return 0;
}

static void save_class(HDEVINFO h, SP_DEVINFO_DATA *d, const wchar_t *guid)
{
    HKEY k = SetupDiCreateDevRegKeyW(h, d, DICS_FLAG_GLOBAL, 0, DIREG_DEV,
                                     NULL, NULL);
    if (k == INVALID_HANDLE_VALUE) {
        return;
    }
    RegSetValueExW(k, SAVED_CLASS, 0, REG_SZ, (const BYTE *)guid,
                   (DWORD)((wcslen(guid) + 1) * sizeof(wchar_t)));
    RegCloseKey(k);
}

static int load_class(HDEVINFO h, SP_DEVINFO_DATA *d, wchar_t *out,
                      DWORD cch)
{
    HKEY k = SetupDiOpenDevRegKey(h, d, DICS_FLAG_GLOBAL, 0, DIREG_DEV,
                                  KEY_READ);
    DWORD type = 0, len = (cch - 1) * (DWORD)sizeof(wchar_t);
    LONG r;

    memset(out, 0, cch * sizeof(wchar_t));
    if (k == INVALID_HANDLE_VALUE) {
        return 0;
    }
    r = RegQueryValueExW(k, SAVED_CLASS, NULL, &type, (BYTE *)out, &len);
    RegCloseKey(k);
    out[cch - 1] = 0;
    return r == ERROR_SUCCESS && type == REG_SZ && out[0];
}

/*
 * Put a USBDevice-class device back in its class from before bind (saved,
 * or USB for a composite device), so a devnode whose removal is deferred
 * loses the WinUSB software key and reinstalls in the right class.
 */
static void restore_class(HDEVINFO h, SP_DEVINFO_DATA *d, J *j)
{
    wchar_t cur[64], orig[64], *compat;

    dev_prop_into(h, d, SPDRP_CLASSGUID, cur, 64);
    if (_wcsicmp(cur, CLS_USBDEVICE) != 0) {
        return;
    }
    if (!load_class(h, d, orig, 64)) {
        compat = dev_prop(h, d, SPDRP_COMPATIBLEIDS);
        if (msz_has_prefix(compat, L"USB\\COMPOSITE")) {
            wcscpy(orig, CLS_USB);
        }
        free(compat);
    }
    if (!orig[0] || !_wcsicmp(orig, CLS_USBDEVICE)) {
        return;
    }
    if (SetupDiSetDeviceRegistryPropertyW(h, d, SPDRP_CLASSGUID,
                                          (const BYTE *)orig,
                                          (DWORD)((wcslen(orig) + 1) *
                                                  sizeof(wchar_t)))) {
        if (j) {
            j_w(j, "class_restored", orig);
        }
    } else if (j) {
        j_err(j, "class_restore", GetLastError());
    }
}

/* User-mode USB drivers a device must not be left on after unbind */
static int is_userdrv(const Info *in)
{
    return is_winusb(in->service) || !_wcsicmp(in->service, L"libusbK") ||
           !_wcsicmp(in->service, L"libusb0") ||
           !_wcsicmp(in->provider, PROVIDER);
}

/* An INF file's text as UTF-16 (UTF-16 with BOM, else ANSI); free() */
static wchar_t *read_inf_text(const wchar_t *path)
{
    HANDLE f = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ, NULL,
                           OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    DWORD size, got = 0;
    BYTE *raw;
    wchar_t *w;
    int n;

    if (f == INVALID_HANDLE_VALUE) {
        return NULL;
    }
    size = GetFileSize(f, NULL);
    if (size == INVALID_FILE_SIZE || size > 4 * 1024 * 1024) {
        CloseHandle(f);
        return NULL;
    }
    raw = calloc(size + 4, 1);
    if (!raw) {
        oom();
    }
    if (!ReadFile(f, raw, size, &got, NULL)) {
        got = 0;
    }
    CloseHandle(f);
    if (got >= 2 && raw[0] == 0xff && raw[1] == 0xfe) {
        w = calloc(got / 2 + 1, sizeof(wchar_t));
        if (!w) {
            oom();
        }
        memcpy(w, raw + 2, got - 2);
        free(raw);
        return w;
    }
    n = MultiByteToWideChar(CP_ACP, 0, (const char *)raw, (int)got, NULL, 0);
    w = calloc((size_t)n + 1, sizeof(wchar_t));
    if (!w) {
        oom();
    }
    MultiByteToWideChar(CP_ACP, 0, (const char *)raw, (int)got, w, n);
    free(raw);
    return w;
}

/*
 * Every published package (%WINDIR%\INF\oem*.inf) naming VID_v&PID_p: the
 * own ones are deleted from the driver store, others only listed in j
 * ("foreign_packages"). The number deleted.
 */
static int sweep_packages(unsigned vid, unsigned pid, J *j)
{
    wchar_t dir[MAX_PATH], pattern[MAX_PATH], path[MAX_PATH], id[32];
    WIN32_FIND_DATAW fd;
    HANDLE find;
    Buf mine = { 0 }, foreign = { 0 };
    int nmine = 0, nforeign = 0;
    UINT wl = GetWindowsDirectoryW(dir, MAX_PATH - 40);

    if (wl == 0 || wl >= MAX_PATH - 40) {
        return 0;
    }
    wcscat(dir, L"\\INF");
    swprintf(pattern, MAX_PATH, L"%ls\\oem*.inf", dir);
    swprintf(id, 32, L"VID_%04X&PID_%04X", vid, pid);
    b_str(&mine, "[");
    b_str(&foreign, "[");
    find = FindFirstFileW(pattern, &fd);
    if (find != INVALID_HANDLE_VALUE) {
        do {
            wchar_t *text, provider[128];
            HINF hinf;
            char *name;

            swprintf(path, MAX_PATH, L"%ls\\%ls", dir, fd.cFileName);
            text = read_inf_text(path);
            if (!text || !wcsistr(text, id)) {
                free(text);
                continue;
            }
            free(text);
            provider[0] = 0;
            hinf = SetupOpenInfFileW(path, NULL, INF_STYLE_WIN4, NULL);
            if (hinf != INVALID_HANDLE_VALUE) {
                if (!SetupGetLineTextW(NULL, hinf, L"Version", L"Provider",
                                       provider, 128, NULL)) {
                    provider[0] = 0;
                }
                SetupCloseInfFile(hinf);
            }
            name = utf8(fd.cFileName);
            if (!_wcsicmp(provider, PROVIDER)) {
                if (sb("SetupUninstallOEMInfW(sweep)",
                       SetupUninstallOEMInfW(fd.cFileName, SUOI_FORCEDELETE,
                                             NULL))) {
                    b_str(&mine, nmine++ ? "," : "");
                    b_jstr(&mine, name);
                }
            } else {
                J e;
                char *str;
                j_open(&e);
                j_s(&e, "inf", name);
                j_w(&e, "provider", provider);
                str = j_take(&e);
                b_str(&foreign, nforeign++ ? "," : "");
                b_str(&foreign, str);
                free(str);
            }
            free(name);
        } while (FindNextFileW(find, &fd));
        FindClose(find);
    }
    b_str(&mine, "]");
    b_str(&foreign, "]");
    if (j) {
        if (nmine) {
            j_raw(j, "packages_removed", mine.p);
        }
        if (nforeign) {
            j_raw(j, "foreign_packages", foreign.p);
        }
    }
    free(mine.p);
    free(foreign.p);
    return nmine;
}

/* ---- third-party USB filters and drivers that take devices ------------- */

#define USBDK_NOTE " (from virt-viewer?) - devices may not return to their " \
                   "Windows drivers"

static void warn(Buf *w, int *nw, const char *msg)
{
    b_str(w, (*nw)++ ? "," : "");
    b_jstr(w, msg);
}

/* Each filter in a multi-sz; UsbDk once, with what it does */
static void warn_filters(const char *where, const wchar_t *msz, Buf *w,
                         int *nw, int *usbdk)
{
    const wchar_t *f;
    char msg[256];

    for (f = msz; f && *f; f += wcslen(f) + 1) {
        char *name = utf8(f);
        if (!_wcsicmp(f, L"UsbDk")) {
            if (!*usbdk) {
                snprintf(msg, sizeof msg, "UsbDk filter installed%s",
                         USBDK_NOTE);
                warn(w, nw, msg);
            }
            *usbdk = 1;
        } else {
            snprintf(msg, sizeof msg, "%s filter %s installed", where, name);
            warn(w, nw, msg);
        }
        free(name);
    }
}

/* A REG_MULTI_SZ value under HKLM, double-NUL terminated; free() */
static wchar_t *reg_msz(const wchar_t *key, const wchar_t *value)
{
    DWORD size = 0;
    wchar_t *buf;

    if (RegGetValueW(HKEY_LOCAL_MACHINE, key, value, RRF_RT_REG_MULTI_SZ,
                     NULL, NULL, &size) != ERROR_SUCCESS || size == 0) {
        return NULL;
    }
    buf = calloc(size + 2 * sizeof(wchar_t), 1);
    if (!buf) {
        oom();
    }
    if (RegGetValueW(HKEY_LOCAL_MACHINE, key, value, RRF_RT_REG_MULTI_SZ,
                     NULL, buf, &size) != ERROR_SUCCESS) {
        free(buf);
        return NULL;
    }
    return buf;
}

/*
 * What can take USB devices away from their Windows drivers: filters on
 * the USB class or on these devices, the UsbDk service, and libusbK,
 * libusb0 or UsbDk packages naming one of them. As a JSON array; free().
 */
static char *hijack_warnings(const Dev *devs, int n)
{
    static const wchar_t *cls_key =
        L"SYSTEM\\CurrentControlSet\\Control\\Class\\"
        L"{36FC9E60-C465-11CF-8056-444553540000}";
    static const wchar_t *drivers[] = { L"libusbK", L"libusb0", L"UsbDk" };
    Buf w = { 0 };
    int nw = 0, usbdk = 0, i;
    wchar_t *f, dir[MAX_PATH], pattern[MAX_PATH], path[MAX_PATH];
    HKEY k;
    WIN32_FIND_DATAW fd;
    HANDLE find;
    UINT wl;
    char msg[256];

    if (!devs) {
        n = 0;
    }
    b_str(&w, "[");
    f = reg_msz(cls_key, L"UpperFilters");
    warn_filters("USB class upper", f, &w, &nw, &usbdk);
    free(f);
    f = reg_msz(cls_key, L"LowerFilters");
    warn_filters("USB class lower", f, &w, &nw, &usbdk);
    free(f);
    if (RegOpenKeyExW(HKEY_LOCAL_MACHINE,
                      L"SYSTEM\\CurrentControlSet\\Services\\UsbDk", 0,
                      KEY_READ, &k) == ERROR_SUCCESS) {
        RegCloseKey(k);
        if (!usbdk) {
            snprintf(msg, sizeof msg, "UsbDk driver installed%s", USBDK_NOTE);
            warn(&w, &nw, msg);
            usbdk = 1;
        }
    }
    for (i = 0; i < n; i++) {
        char where[48];
        snprintf(where, sizeof where, "%04x:%04x upper", devs[i].vid,
                 devs[i].pid);
        f = cm_prop(devs[i].dn, CM_DRP_UPPERFILTERS);
        warn_filters(where, f, &w, &nw, &usbdk);
        free(f);
        snprintf(where, sizeof where, "%04x:%04x lower", devs[i].vid,
                 devs[i].pid);
        f = cm_prop(devs[i].dn, CM_DRP_LOWERFILTERS);
        warn_filters(where, f, &w, &nw, &usbdk);
        free(f);
    }

    wl = GetWindowsDirectoryW(dir, MAX_PATH - 40);
    if (wl && wl < MAX_PATH - 40 && n > 0) {
        wcscat(dir, L"\\INF");
        swprintf(pattern, MAX_PATH, L"%ls\\oem*.inf", dir);
        find = FindFirstFileW(pattern, &fd);
        if (find != INVALID_HANDLE_VALUE) {
            do {
                wchar_t *text;
                const wchar_t *drv = NULL;
                size_t d;

                swprintf(path, MAX_PATH, L"%ls\\%ls", dir, fd.cFileName);
                text = read_inf_text(path);
                for (d = 0; text && !drv && d < 3; d++) {
                    if (wcsistr(text, drivers[d])) {
                        drv = drivers[d];
                    }
                }
                for (i = 0; drv && i < n; i++) {
                    wchar_t id[32];
                    int seen = 0, k2;
                    for (k2 = 0; k2 < i; k2++) {
                        seen |= devs[k2].vid == devs[i].vid &&
                                devs[k2].pid == devs[i].pid;
                    }
                    swprintf(id, 32, L"VID_%04X&PID_%04X", devs[i].vid,
                             devs[i].pid);
                    if (!seen && wcsistr(text, id)) {
                        char *nm = utf8(fd.cFileName), *dn = utf8(drv);
                        snprintf(msg, sizeof msg,
                                 "%04x:%04x: driver package %s installs %s",
                                 devs[i].vid, devs[i].pid, nm, dn);
                        warn(&w, &nw, msg);
                        free(nm);
                        free(dn);
                    }
                }
                free(text);
            } while (FindNextFileW(find, &fd));
            FindClose(find);
        }
    }
    b_str(&w, "]");
    return w.p;
}

/*
 * Remove the devnode so Windows matches a driver afresh; rescan unless the
 * removal is deferred (*reboot set). With a *sweep* id (vid << 16 | pid),
 * every own package naming the device leaves the driver store first.
 */
static int remove_dev(const wchar_t *inst, const wchar_t *oem_inf, J *j,
                      BOOL *reboot, DWORD *err, long sweep)
{
    DiUninstallDevice_fn uninstall =
        (DiUninstallDevice_fn)(void (*)(void))newdev_fn("DiUninstallDevice");
    SP_DEVINFO_DATA d;
    HDEVINFO h;
    DEVINST parent = 0;
    BOOL rb = FALSE;

    *err = 0;
    if (!uninstall) {
        *err = GetLastError();
        return 0;
    }
    h = open_dev(inst, &d);
    if (h == INVALID_HANDLE_VALUE) {
        *err = GetLastError();
        return 0;
    }
    if (CM_Get_Parent(&parent, d.DevInst, 0) != CR_SUCCESS) {
        parent = 0;
    }
    restore_class(h, &d, j);
    if (!sb("DiUninstallDevice", uninstall(NULL, h, &d, 0, &rb))) {
        *err = GetLastError();
        SetupDiDestroyDeviceInfoList(h);
        return 0;
    }
    SetupDiDestroyDeviceInfoList(h);
    if (rb) {
        snote("DiUninstallDevice", "removal deferred");
    }
    /* Before the rescan, or Windows picks the package again */
    if (sweep >= 0) {
        sweep_packages((unsigned)(sweep >> 16) & 0xffff,
                       (unsigned)sweep & 0xffff, j);
    }
    if (oem_inf && oem_inf[0]) {
        char *s = utf8(oem_inf);
        sb("SetupUninstallOEMInfW",
           SetupUninstallOEMInfW(oem_inf, SUOI_FORCEDELETE, NULL));
        if (j) {
            j_s(j, "oem_inf_removed", s);
        }
        free(s);
    }
    if (rb) {
        *reboot = TRUE;
        return 1;
    }
    rescan(parent);
    return 1;
}

#define WANT_WINDOWS 0      /* running on a driver other than WinUSB */
#define WANT_WINUSB  1      /* running on WinUSB */
#define WANT_ANY     2      /* running */
static int wait_back(const wchar_t *inst, unsigned vid, unsigned pid,
                     Info *in, wchar_t *now, int seconds, int want);

/* Undo a half-done bind: remove the devnode, report what Windows put back. */
static void restore(J *j, const Dev *dev, BOOL *reboot)
{
    DWORD err = 0;
    Info in;
    wchar_t now[MAX_DEVICE_ID_LEN];

    if (!remove_dev(dev->inst, NULL, j, reboot, &err,
                    (long)(dev->vid << 16 | dev->pid))) {
        j_b(j, "restored", 0);
        j_err(j, "restore_detail", err);
        return;
    }
    if (*reboot) {
        j_b(j, "restored", 0);
        return;
    }
    j_b(j, "restored", wait_back(dev->inst, dev->vid, dev->pid, &in, now, 30,
                                 WANT_WINDOWS));
    j_w(j, "service_after", in.service);
}

/* ok; "pending_reboot" when Windows finishes the change only on replug */
static int done(J *j, BOOL reboot)
{
    j_b(j, "reboot", reboot);
    j_b(j, "ok", 1);
    if (reboot) {
        j_s(j, "status", "pending_reboot");
        j_s(j, "message", PENDING_MSG);
        return finish(j, EXIT_REBOOT);
    }
    j_s(j, "status", "done");
    return finish(j, EXIT_DONE);
}

/* ---- own INF: a signed package naming this device ---------------------
 *
 * For a device the inbox winusb.inf node does not take (USB storage), a
 * package whose INF names USB\VID_v&PID_p, class USBDevice, and installs
 * Windows' own WinUSB through Include/Needs. Its catalog is signed with a
 * self-signed code-signing certificate made for this device; the private
 * key is deleted right after signing, and the certificate is put in the
 * machine's Root and TrustedPublisher stores. unbind removes package and
 * certificate.
 */

#define INSTALLFLAG_FORCE_ 0x00000001

typedef BOOL (WINAPI *UpdateDriver_fn)(HWND, LPCWSTR, LPCWSTR, DWORD, PBOOL);

/* mssign32.dll (no header in mingw-w64) */
typedef struct {
    DWORD cbSize;
    LPCWSTR pwszFileName;
    HANDLE hFile;
} SIGNER_FILE_INFO_;
typedef struct {
    DWORD cbSize;
    DWORD *pdwIndex;
    DWORD dwSubjectChoice;
    union {
        SIGNER_FILE_INFO_ *pSignerFileInfo;
        void *pSignerBlobInfo;
    } u;
} SIGNER_SUBJECT_INFO_;
typedef struct {
    DWORD cbSize;
    PCCERT_CONTEXT pSigningCert;
    DWORD dwCertPolicy;
    HCERTSTORE hCertStore;
} SIGNER_CERT_STORE_INFO_;
typedef struct {
    DWORD cbSize;
    DWORD dwCertChoice;
    union {
        LPCWSTR pwszSpcFile;
        SIGNER_CERT_STORE_INFO_ *pCertStoreInfo;
        void *pSpcChainInfo;
    } u;
    HWND hwnd;
} SIGNER_CERT_;
typedef struct {
    DWORD cbSize;
    ALG_ID algidHash;
    DWORD dwAttrChoice;
    union {
        void *pAttrAuthcode;
    } u;
    PCRYPT_ATTRIBUTES psAuthenticated;
    PCRYPT_ATTRIBUTES psUnauthenticated;
} SIGNER_SIGNATURE_INFO_;
typedef struct {
    DWORD cbSize;
    DWORD cbBlob;
    BYTE *pbBlob;
} SIGNER_CONTEXT_;
#define SIGNER_SUBJECT_FILE_      1
#define SIGNER_CERT_STORE_        2
#define SIGNER_CERT_POLICY_CHAIN_ 2
#define SIGNER_NO_ATTR_           0
typedef HRESULT (WINAPI *SignerSignEx_fn)(DWORD, SIGNER_SUBJECT_INFO_ *,
                                          SIGNER_CERT_ *,
                                          SIGNER_SIGNATURE_INFO_ *, void *,
                                          LPCWSTR, PCRYPT_ATTRIBUTES, void *,
                                          SIGNER_CONTEXT_ **);
typedef HRESULT (WINAPI *SignerFreeSignerContext_fn)(SIGNER_CONTEXT_ *);

/* Catalog member subject type of an INF (flat file) */
static const GUID INF_SUBJECT = {
    0xde351a42, 0x8e59, 0x11d0,
    { 0x8c, 0x47, 0x00, 0xc0, 0x4f, 0xc2, 0x95, 0xee } };

/* "winusb-switch vvvv:pppp": certificate subject and key container */
static void cert_name(wchar_t *out, size_t cch, unsigned vid, unsigned pid)
{
    swprintf(out, cch, PROVIDER L" %04x:%04x", vid, pid);
}

static void hex(char *out, const BYTE *p, DWORD n)
{
    DWORD i;
    for (i = 0; i < n; i++) {
        snprintf(out + 2 * i, 3, "%02X", p[i]);
    }
    out[2 * n] = 0;
}

static void thumbprint(PCCERT_CONTEXT c, char *out)
{
    BYTE h[20];
    DWORD n = sizeof h;

    out[0] = 0;
    if (CertGetCertificateContextProperty(c, CERT_SHA1_HASH_PROP_ID, h, &n)) {
        hex(out, h, n);
    }
}

/*
 * Remove every certificate whose subject holds *match* from the machine's
 * Root and TrustedPublisher stores; their thumbprints into j under key.
 */
static int cert_remove(const wchar_t *match, J *j, const char *key)
{
    static const wchar_t *stores[] = { L"Root", L"TrustedPublisher" };
    Buf list = { 0 };
    int n = 0, i;

    b_str(&list, "[");
    for (i = 0; i < 2; i++) {
        HCERTSTORE st = CertOpenStore(CERT_STORE_PROV_SYSTEM_W, 0, 0,
                                      CERT_SYSTEM_STORE_LOCAL_MACHINE,
                                      stores[i]);
        PCCERT_CONTEXT c;

        if (!st) {
            sb("CertOpenStore", FALSE);
            continue;
        }
        while ((c = CertFindCertificateInStore(st, X509_ASN_ENCODING, 0,
                                               CERT_FIND_SUBJECT_STR_W,
                                               match, NULL)) != NULL) {
            char t[48];
            thumbprint(c, t);
            /* frees c */
            if (!sb("CertDeleteCertificateFromStore",
                    CertDeleteCertificateFromStore(c))) {
                break;
            }
            if (i == 0) {
                b_str(&list, n++ ? "," : "");
                b_jstr(&list, t);
            }
        }
        CertCloseStore(st, 0);
    }
    b_str(&list, "]");
    if (j) {
        j_raw(j, key, list.p);
    }
    free(list.p);
    return n;
}

static void drop_key(const wchar_t *name)
{
    HCRYPTPROV p = 0;
    sb("CryptAcquireContextW(DELETEKEYSET)",
       CryptAcquireContextW(&p, name, MS_ENH_RSA_AES_PROV_W, PROV_RSA_AES,
                            CRYPT_MACHINE_KEYSET | CRYPT_DELETEKEYSET |
                            CRYPT_SILENT));
}

/* Self-signed, code signing only, key in a machine key container *name* */
static PCCERT_CONTEXT make_cert(const wchar_t *name)
{
    HCRYPTPROV prov = 0;
    HCRYPTKEY key = 0;
    CERT_NAME_BLOB subj = { 0, NULL };
    CRYPT_KEY_PROV_INFO kpi;
    CRYPT_ALGORITHM_IDENTIFIER alg;
    SYSTEMTIME end = { 2049, 12, 0, 31, 0, 0, 0, 0 };
    LPSTR eku_oid = szOID_PKIX_KP_CODE_SIGNING;
    CERT_ENHKEY_USAGE eku = { 1, &eku_oid };
    CERT_EXTENSION ext;
    CERT_EXTENSIONS exts;
    BYTE *ekub = NULL;
    DWORD n = 0;
    wchar_t x500[96];
    PCCERT_CONTEXT c = NULL;

    /* A container left by an earlier, interrupted bind */
    CryptAcquireContextW(&prov, name, MS_ENH_RSA_AES_PROV_W, PROV_RSA_AES,
                         CRYPT_MACHINE_KEYSET | CRYPT_DELETEKEYSET |
                         CRYPT_SILENT);
    prov = 0;
    if (!sb("CryptAcquireContextW(NEWKEYSET)",
            CryptAcquireContextW(&prov, name, MS_ENH_RSA_AES_PROV_W,
                                 PROV_RSA_AES,
                                 CRYPT_NEWKEYSET | CRYPT_MACHINE_KEYSET |
                                 CRYPT_SILENT))) {
        prov = 0;
        goto out;
    }
    if (!sb("CryptGenKey", CryptGenKey(prov, AT_SIGNATURE, 2048u << 16,
                                       &key))) {
        goto out;
    }
    swprintf(x500, 96, L"CN=\"%ls\"", name);
    if (!sb("CertStrToNameW", CertStrToNameW(X509_ASN_ENCODING, x500,
                                             CERT_X500_NAME_STR, NULL, NULL,
                                             &subj.cbData, NULL))) {
        goto out;
    }
    subj.pbData = malloc(subj.cbData);
    if (!subj.pbData) {
        oom();
    }
    if (!CertStrToNameW(X509_ASN_ENCODING, x500, CERT_X500_NAME_STR, NULL,
                        subj.pbData, &subj.cbData, NULL)) {
        goto out;
    }
    if (!sb("CryptEncodeObject(EKU)",
            CryptEncodeObject(X509_ASN_ENCODING, X509_ENHANCED_KEY_USAGE,
                              &eku, NULL, &n))) {
        goto out;
    }
    ekub = malloc(n);
    if (!ekub) {
        oom();
    }
    if (!CryptEncodeObject(X509_ASN_ENCODING, X509_ENHANCED_KEY_USAGE, &eku,
                           ekub, &n)) {
        goto out;
    }
    ext.pszObjId = szOID_ENHANCED_KEY_USAGE;
    ext.fCritical = FALSE;
    ext.Value.cbData = n;
    ext.Value.pbData = ekub;
    exts.cExtension = 1;
    exts.rgExtension = &ext;
    memset(&kpi, 0, sizeof kpi);
    kpi.pwszContainerName = (LPWSTR)name;
    kpi.pwszProvName = (LPWSTR)MS_ENH_RSA_AES_PROV_W;
    kpi.dwProvType = PROV_RSA_AES;
    kpi.dwFlags = CRYPT_MACHINE_KEYSET;
    kpi.dwKeySpec = AT_SIGNATURE;
    memset(&alg, 0, sizeof alg);
    alg.pszObjId = szOID_RSA_SHA256RSA;
    c = CertCreateSelfSignCertificate(0, &subj, 0, &kpi, &alg, NULL, &end,
                                      &exts);
    sb("CertCreateSelfSignCertificate", c != NULL);

out:
    free(ekub);
    free(subj.pbData);
    if (key) {
        CryptDestroyKey(key);
    }
    if (prov) {
        CryptReleaseContext(prov, 0);
    }
    return c;
}

/* Add the certificate (without its key) to Root and TrustedPublisher */
static int trust_cert(PCCERT_CONTEXT c, const wchar_t *name)
{
    static const wchar_t *stores[] = { L"Root", L"TrustedPublisher" };
    CRYPT_DATA_BLOB friendly;
    int i, ok = 1;

    friendly.cbData = (DWORD)((wcslen(name) + 1) * sizeof(wchar_t));
    friendly.pbData = (BYTE *)name;
    for (i = 0; i < 2; i++) {
        HCERTSTORE st = CertOpenStore(CERT_STORE_PROV_SYSTEM_W, 0, 0,
                                      CERT_SYSTEM_STORE_LOCAL_MACHINE,
                                      stores[i]);
        PCCERT_CONTEXT added = NULL;

        if (!sb("CertOpenStore", st != NULL)) {
            ok = 0;
            continue;
        }
        if (sb("CertAddEncodedCertificateToStore",
               CertAddEncodedCertificateToStore(st, X509_ASN_ENCODING,
                                                c->pbCertEncoded,
                                                c->cbCertEncoded,
                                                CERT_STORE_ADD_REPLACE_EXISTING,
                                                &added))) {
            CertSetCertificateContextProperty(added, CERT_FRIENDLY_NAME_PROP_ID,
                                              0, &friendly);
            CertFreeCertificateContext(added);
        } else {
            ok = 0;
        }
        CertCloseStore(st, 0);
    }
    return ok;
}

static CRYPTCATATTRIBUTE *cat_attr(HANDLE cat, CRYPTCATMEMBER *m,
                                   const wchar_t *name, const wchar_t *value)
{
    DWORD flags = CRYPTCAT_ATTR_AUTHENTICATED | CRYPTCAT_ATTR_NAMEASCII |
                  CRYPTCAT_ATTR_DATAASCII;
    DWORD size = (DWORD)((wcslen(value) + 1) * sizeof(wchar_t));

    if (m) {
        return CryptCATPutAttrInfo(cat, m, (WCHAR *)name, flags, size,
                                   (BYTE *)value);
    }
    return CryptCATPutCatAttrInfo(cat, (WCHAR *)name, flags, size,
                                  (BYTE *)value);
}

/* A catalog holding the INF's hash, as MakeCat would write it */
static int make_cat(const wchar_t *cat_path, const wchar_t *inf_path,
                    const wchar_t *inf_name, const wchar_t *hwid)
{
    HCRYPTPROV prov = 0;
    HANDLE cat = INVALID_HANDLE_VALUE, f;
    BYTE hash[64], enc[128];
    DWORD cb = sizeof hash, cbenc = sizeof enc;
    wchar_t tag[2 * 64 + 1], lower_hwid[64], lower_name[MAX_PATH];
    char tag8[2 * 64 + 1];
    SPC_LINK link;
    SIP_INDIRECT_DATA sip;
    CRYPTCATMEMBER *m;
    int ok = 0;
    size_t i;

    wcsncpy(lower_hwid, hwid, 63);
    lower_hwid[63] = 0;
    _wcslwr(lower_hwid);
    wcsncpy(lower_name, inf_name, MAX_PATH - 1);
    lower_name[MAX_PATH - 1] = 0;
    _wcslwr(lower_name);

    f = CreateFileW(inf_path, GENERIC_READ, FILE_SHARE_READ, NULL,
                    OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    if (!sb("CreateFileW(inf)", f != INVALID_HANDLE_VALUE)) {
        return 0;
    }
    ok = sb("CryptCATAdminCalcHashFromFileHandle",
            CryptCATAdminCalcHashFromFileHandle(f, &cb, hash, 0));
    CloseHandle(f);
    if (!ok) {
        return 0;
    }
    ok = 0;
    hex(tag8, hash, cb);
    for (i = 0; tag8[i]; i++) {
        tag[i] = (wchar_t)tag8[i];
    }
    tag[i] = 0;

    if (!sb("CryptAcquireContextW(VERIFYCONTEXT)",
            CryptAcquireContextW(&prov, NULL, NULL, PROV_RSA_FULL,
                                 CRYPT_VERIFYCONTEXT))) {
        return 0;
    }
    cat = CryptCATOpen((LPWSTR)cat_path, CRYPTCAT_OPEN_CREATENEW, prov, 0, 0);
    if (!sb("CryptCATOpen", cat != INVALID_HANDLE_VALUE)) {
        goto out;
    }
    if (!sb("CryptCATPutCatAttrInfo",
            cat_attr(cat, NULL, L"HWID1", lower_hwid) != NULL &&
            cat_attr(cat, NULL, L"OS", L"10_X64,10_ARM64") != NULL)) {
        goto out;
    }
    link.dwLinkChoice = SPC_FILE_LINK_CHOICE;
    link.pwszFile = (LPWSTR)L"<<<Obsolete>>>";
    if (!sb("CryptEncodeObject(SPC_LINK)",
            CryptEncodeObject(X509_ASN_ENCODING, SPC_CAB_DATA_OBJID, &link,
                              enc, &cbenc))) {
        goto out;
    }
    memset(&sip, 0, sizeof sip);
    sip.Data.pszObjId = (LPSTR)SPC_CAB_DATA_OBJID;
    sip.Data.Value.cbData = cbenc;
    sip.Data.Value.pbData = enc;
    sip.DigestAlgorithm.pszObjId = (LPSTR)szOID_OIWSEC_sha1;
    sip.Digest.cbData = cb;
    sip.Digest.pbData = hash;
    m = CryptCATPutMemberInfo(cat, NULL, tag, (GUID *)&INF_SUBJECT, 0x200,
                              sizeof sip, (BYTE *)&sip);
    if (!sb("CryptCATPutMemberInfo", m != NULL)) {
        goto out;
    }
    if (!sb("CryptCATPutAttrInfo",
            cat_attr(cat, m, L"File", lower_name) != NULL &&
            cat_attr(cat, m, L"OSAttr", L"2:6.1,2:6.2,2:6.3,2:10.0") !=
            NULL)) {
        goto out;
    }
    ok = sb("CryptCATPersistStore", CryptCATPersistStore(cat));

out:
    if (cat != INVALID_HANDLE_VALUE) {
        CryptCATClose(cat);
    }
    CryptReleaseContext(prov, 0);
    return ok;
}

/* Authenticode-sign the catalog with c (SHA-256) */
static int sign_cat(const wchar_t *cat_path, PCCERT_CONTEXT c)
{
    /* Authenticode attributes: SpcSpOpusInfo {}, statement type individual */
    static BYTE opus[] = { 0x30, 0x00 };
    static BYTE stmt[] = { 0x30, 0x0c, 0x06, 0x0a, 0x2b, 0x06, 0x01, 0x04,
                           0x01, 0x82, 0x37, 0x02, 0x01, 0x15 };
    HMODULE m = LoadLibraryExW(L"mssign32.dll", NULL,
                               LOAD_LIBRARY_SEARCH_SYSTEM32);
    SignerSignEx_fn sign;
    SignerFreeSignerContext_fn free_ctx;
    SIGNER_FILE_INFO_ fi;
    SIGNER_SUBJECT_INFO_ si;
    SIGNER_CERT_STORE_INFO_ csi;
    SIGNER_CERT_ sc;
    SIGNER_SIGNATURE_INFO_ sig;
    SIGNER_CONTEXT_ *ctx = NULL;
    CRYPT_ATTR_BLOB opus_blob = { sizeof opus, opus };
    CRYPT_ATTR_BLOB stmt_blob = { sizeof stmt, stmt };
    CRYPT_ATTRIBUTE attrs[2];
    CRYPT_ATTRIBUTES attr_array;
    DWORD index = 0;
    HRESULT hr;

    if (!sb("LoadLibraryExW(mssign32)", m != NULL)) {
        return 0;
    }
    sign = (SignerSignEx_fn)(void (*)(void))GetProcAddress(m, "SignerSignEx");
    free_ctx = (SignerFreeSignerContext_fn)(void (*)(void))
               GetProcAddress(m, "SignerFreeSignerContext");
    if (!sb("GetProcAddress(SignerSignEx)", sign != NULL)) {
        FreeLibrary(m);
        return 0;
    }
    memset(&fi, 0, sizeof fi);
    fi.cbSize = sizeof fi;
    fi.pwszFileName = cat_path;
    memset(&si, 0, sizeof si);
    si.cbSize = sizeof si;
    si.pdwIndex = &index;
    si.dwSubjectChoice = SIGNER_SUBJECT_FILE_;
    si.u.pSignerFileInfo = &fi;
    memset(&csi, 0, sizeof csi);
    csi.cbSize = sizeof csi;
    csi.pSigningCert = c;
    csi.dwCertPolicy = SIGNER_CERT_POLICY_CHAIN_;
    memset(&sc, 0, sizeof sc);
    sc.cbSize = sizeof sc;
    sc.dwCertChoice = SIGNER_CERT_STORE_;
    sc.u.pCertStoreInfo = &csi;
    attrs[0].pszObjId = (LPSTR)SPC_SP_OPUS_INFO_OBJID;
    attrs[0].cValue = 1;
    attrs[0].rgValue = &opus_blob;
    attrs[1].pszObjId = (LPSTR)SPC_STATEMENT_TYPE_OBJID;
    attrs[1].cValue = 1;
    attrs[1].rgValue = &stmt_blob;
    attr_array.cAttr = 2;
    attr_array.rgAttr = attrs;
    memset(&sig, 0, sizeof sig);
    sig.cbSize = sizeof sig;
    sig.algidHash = CALG_SHA_256;
    sig.dwAttrChoice = SIGNER_NO_ATTR_;
    sig.psAuthenticated = &attr_array;
    hr = shr("SignerSignEx", sign(0, &si, &sc, &sig, NULL, NULL, NULL, NULL,
                                  &ctx));
    if (ctx && free_ctx) {
        free_ctx(ctx);
    }
    FreeLibrary(m);
    return hr == S_OK;
}

/* A fresh random GUID as {XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX} */
static int new_guid(char *out, size_t n)
{
    HCRYPTPROV p = 0;
    BYTE b[16];

    if (!CryptAcquireContextW(&p, NULL, NULL, PROV_RSA_FULL,
                              CRYPT_VERIFYCONTEXT)) {
        return 0;
    }
    if (!CryptGenRandom(p, sizeof b, b)) {
        CryptReleaseContext(p, 0);
        return 0;
    }
    CryptReleaseContext(p, 0);
    b[6] = (BYTE)((b[6] & 0x0f) | 0x40);
    b[8] = (BYTE)((b[8] & 0x3f) | 0x80);
    snprintf(out, n, "{%02X%02X%02X%02X-%02X%02X-%02X%02X-%02X%02X-"
             "%02X%02X%02X%02X%02X%02X}",
             b[0], b[1], b[2], b[3], b[4], b[5], b[6], b[7], b[8], b[9],
             b[10], b[11], b[12], b[13], b[14], b[15]);
    return 1;
}

static int write_inf(const wchar_t *path, const char *cat_name,
                     unsigned vid, unsigned pid, J *j)
{
    char guid[48], text[2048];
    SYSTEMTIME now;
    HANDLE f;
    DWORD wrote = 0;
    int n;

    if (!sb("CryptGenRandom(interface GUID)", new_guid(guid, sizeof guid))) {
        return 0;
    }
    j_s(j, "interface_guid", guid);
    GetLocalTime(&now);
    n = snprintf(text, sizeof text,
        "; winusb-switch: Windows' WinUSB for USB\\VID_%04X&PID_%04X\r\n"
        "[Version]\r\n"
        "Signature   = \"$Windows NT$\"\r\n"
        "Class       = USBDevice\r\n"
        "ClassGuid   = {88BAE032-5A81-49F0-BC3D-A4FF138216D6}\r\n"
        "Provider    = %%Provider%%\r\n"
        "CatalogFile = %s\r\n"
        "DriverVer   = %02u/%02u/%04u,1.0.0.0\r\n"
        "PnpLockdown = 1\r\n"
        "\r\n"
        "[Manufacturer]\r\n"
        "%%Provider%% = Devices,NTamd64,NTarm64\r\n"
        "\r\n"
        "[Devices.NTamd64]\r\n"
        "%%DeviceName%% = USB_Install,USB\\VID_%04X&PID_%04X\r\n"
        "\r\n"
        "[Devices.NTarm64]\r\n"
        "%%DeviceName%% = USB_Install,USB\\VID_%04X&PID_%04X\r\n"
        "\r\n"
        "[USB_Install]\r\n"
        "Include = winusb.inf\r\n"
        "Needs   = WINUSB.NT\r\n"
        "\r\n"
        "[USB_Install.Services]\r\n"
        "Include = winusb.inf\r\n"
        "Needs   = WINUSB.NT.Services\r\n"
        "\r\n"
        "[USB_Install.HW]\r\n"
        "AddReg = Dev_AddReg\r\n"
        "\r\n"
        "[Dev_AddReg]\r\n"
        "HKR,,DeviceInterfaceGUIDs,0x10000,\"%s\"\r\n"
        "\r\n"
        "[Strings]\r\n"
        "Provider   = \"winusb-switch\"\r\n"
        "DeviceName = \"USB device %04x:%04x for QEMU (WinUSB)\"\r\n",
        vid, pid, cat_name, now.wMonth, now.wDay, now.wYear,
        vid, pid, vid, pid, guid, vid, pid);
    if (n <= 0 || n >= (int)sizeof text) {
        return 0;
    }
    f = CreateFileW(path, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS,
                    FILE_ATTRIBUTE_NORMAL, NULL);
    if (!sb("CreateFileW(write inf)", f != INVALID_HANDLE_VALUE)) {
        return 0;
    }
    n = sb("WriteFile(inf)", WriteFile(f, text, (DWORD)n, &wrote, NULL) &&
                             wrote == (DWORD)n);
    CloseHandle(f);
    return n;
}

/* The published name (oemNN.inf) of a package already in the store */
static int oem_name(const wchar_t *inf_path, wchar_t *out, DWORD cch)
{
    PWSTR comp = NULL;

    out[0] = 0;
    if (SetupCopyOEMInfW(inf_path, NULL, SPOST_PATH, SP_COPY_NOOVERWRITE, out,
                         cch, NULL, &comp) ||
        GetLastError() == ERROR_FILE_EXISTS) {
        if (comp && comp != out) {
            memmove(out, comp, (wcslen(comp) + 1) * sizeof(wchar_t));
        }
        return out[0] != 0;
    }
    out[0] = 0;
    return 0;
}

/*
 * Install the own package on the device and see it running on WinUSB.
 * 1 done (after filled); 0 failed, with package and certificate removed.
 */
static int inf_bind(const Dev *dev, const Info *before, J *j, BOOL *reboot,
                    Info *after)
{
    UpdateDriver_fn update = (UpdateDriver_fn)(void (*)(void))
                             newdev_fn("UpdateDriverForPlugAndPlayDevicesW");
    wchar_t dir[MAX_PATH], inf[MAX_PATH], cat[MAX_PATH], name[64], hwid[64];
    wchar_t inf_name[40], cat_name_w[40], oem[MAX_PATH], now[MAX_DEVICE_ID_LEN];
    char cat_name[40], tp[48];
    PCCERT_CONTEXT cert = NULL;
    SP_DEVINFO_DATA d;
    HDEVINFO h;
    BOOL rb = FALSE;
    DWORD err = 0;
    int ok = 0, signed_ok, installed;

    oem[0] = 0;
    cert_name(name, 64, dev->vid, dev->pid);
    swprintf(hwid, 64, L"USB\\VID_%04X&PID_%04X", dev->vid, dev->pid);
    swprintf(inf_name, 40, L"qemu_winusb_%04x_%04x.inf", dev->vid, dev->pid);
    swprintf(cat_name_w, 40, L"qemu_winusb_%04x_%04x.cat", dev->vid, dev->pid);
    snprintf(cat_name, sizeof cat_name, "qemu_winusb_%04x_%04x.cat",
             dev->vid, dev->pid);
    j_s(j, "method", "inf");
    /* Earlier packages for this device would compete with the new one */
    sweep_packages(dev->vid, dev->pid, j);

    if (!update) {
        sb("GetProcAddress(UpdateDriverForPlugAndPlayDevicesW)", FALSE);
        return 0;
    }
    /* The class to return to at unbind */
    h = open_dev(dev->inst, &d);
    if (h != INVALID_HANDLE_VALUE) {
        wchar_t saved[64];
        if (!load_class(h, &d, saved, 64) && before->clsguid[0] &&
            _wcsicmp(before->clsguid, CLS_USBDEVICE) != 0) {
            save_class(h, &d, before->clsguid);
        }
        SetupDiDestroyDeviceInfoList(h);
    }

    if (!GetTempPathW(MAX_PATH - 64, dir)) {
        sb("GetTempPathW", FALSE);
        return 0;
    }
    swprintf(dir + wcslen(dir), 64, L"winusb-switch-%04x-%04x", dev->vid,
             dev->pid);
    CreateDirectoryW(dir, NULL);
    swprintf(inf, MAX_PATH, L"%ls\\%ls", dir, inf_name);
    swprintf(cat, MAX_PATH, L"%ls\\%ls", dir, cat_name_w);
    DeleteFileW(cat);

    if (!write_inf(inf, cat_name, dev->vid, dev->pid, j) ||
        !make_cat(cat, inf, inf_name, hwid)) {
        goto out;
    }
    cert = make_cert(name);
    if (!cert) {
        drop_key(name);
        goto out;
    }
    if (!trust_cert(cert, name)) {
        drop_key(name);
        goto out;
    }
    signed_ok = sign_cat(cat, cert);
    drop_key(name);                         /* never keep a trusted key */
    if (!signed_ok) {
        goto out;
    }
    thumbprint(cert, tp);
    j_s(j, "cert_thumbprint", tp);

    installed = sb("UpdateDriverForPlugAndPlayDevicesW",
                   update(NULL, hwid, inf, INSTALLFLAG_FORCE_, &rb));
    if (!installed) {
        err = GetLastError();
        /* Held for eject, or not matched: stage it for the next start */
        installed = sb("SetupCopyOEMInfW",
                       SetupCopyOEMInfW(inf, NULL, SPOST_PATH, 0, NULL, 0,
                                        NULL, NULL));
    }
    if (installed && oem_name(inf, oem, MAX_PATH)) {
        char *s = utf8(oem);
        j_s(j, "oem_inf", s);
        free(s);
    }
    if (!installed) {
        j_err(j, "install_detail", err);
        goto out;
    }
    if (rb) {
        snote("UpdateDriverForPlugAndPlayDevicesW", "restart requested");
    }

    /* Running on the new package, or restart it by a fresh enumeration */
    get_info(dev->inst, after);
    if (!(after->started && is_winusb(after->service) &&
          !_wcsicmp(after->provider, PROVIDER))) {
        sstate("state after install", dev->dn);
        if (!remove_dev(dev->inst, NULL, NULL, reboot, &err, -1)) {
            goto out;
        }
        if (*reboot) {
            ok = 1;                 /* WinUSB takes it on replug */
            goto out;
        }
        if (!wait_back(dev->inst, dev->vid, dev->pid, after, now, 30,
                       WANT_WINUSB)) {
            goto out;
        }
    }
    ok = is_winusb(after->service) && !_wcsicmp(after->provider, PROVIDER);

out:
    if (!ok) {
        if (oem[0]) {
            sb("SetupUninstallOEMInfW",
               SetupUninstallOEMInfW(oem, SUOI_FORCEDELETE, NULL));
        }
        cert_remove(name, j, "certs_removed");
    }
    if (cert) {
        CertFreeCertificateContext(cert);
    }
    DeleteFileW(inf);
    DeleteFileW(cat);
    RemoveDirectoryW(dir);
    return ok;
}

/* USB mass storage: the inbox winusb.inf node does not take it */
static int is_storage(const wchar_t *inst, const Info *in)
{
    SP_DEVINFO_DATA d;
    HDEVINFO h;
    wchar_t *compat = NULL;
    int r;

    if (!_wcsicmp(in->service, L"USBSTOR") ||
        !_wcsicmp(in->service, L"UASPStor")) {
        return 1;
    }
    h = open_dev(inst, &d);
    if (h == INVALID_HANDLE_VALUE) {
        return 0;
    }
    compat = dev_prop(h, &d, SPDRP_COMPATIBLEIDS);
    SetupDiDestroyDeviceInfoList(h);
    r = msz_has_prefix(compat, L"USB\\Class_08");
    free(compat);
    return r;
}

/* ---- bind -------------------------------------------------------------- */

/* Is this driver node from winusb.inf? Its hardware ID into hwid. */
static int winusb_node(HDEVINFO h, SP_DEVINFO_DATA *d, SP_DRVINFO_DATA_W *drv,
                       wchar_t *hwid, size_t cch)
{
    SP_DRVINFO_DETAIL_DATA_W *det;
    DWORD need = 0;
    const wchar_t *base;
    int r = 0;

    if (!SetupDiGetDriverInfoDetailW(h, d, drv, NULL, 0, &need) &&
        GetLastError() != ERROR_INSUFFICIENT_BUFFER) {
        return 0;
    }
    if (need < sizeof *det) {
        need = sizeof *det;
    }
    det = calloc(need + 2 * sizeof(wchar_t), 1);
    if (!det) {
        oom();
    }
    det->cbSize = sizeof *det;
    if (SetupDiGetDriverInfoDetailW(h, d, drv, det, need, NULL)) {
        base = wcsrchr(det->InfFileName, L'\\');
        base = base ? base + 1 : det->InfFileName;
        if (!_wcsicmp(base, L"winusb.inf")) {
            wcsncpy(hwid, det->HardwareID, cch - 1);
            hwid[cch - 1] = 0;
            r = 1;
        }
    }
    free(det);
    return r;
}

/*
 * Build a driver list of this type from winusb.inf alone and pick the
 * USB\MS_COMP_WINUSB node (any winusb.inf node if that ID is absent).
 * 1 with drv set and the list kept, 0 not found (list destroyed),
 * -1 error.
 */
static int find_winusb(HDEVINFO h, SP_DEVINFO_DATA *d, DWORD type,
                       const wchar_t *inf, SP_DRVINFO_DATA_W *drv,
                       DWORD *err)
{
    SP_DEVINSTALL_PARAMS_W ip;
    SP_DRVINFO_DATA_W cur;
    wchar_t hwid[128];
    DWORD i;
    int found = 0;

    ip.cbSize = sizeof ip;
    if (!SetupDiGetDeviceInstallParamsW(h, d, &ip)) {
        *err = GetLastError();
        return -1;
    }
    ip.Flags |= DI_ENUMSINGLEINF;
    ip.FlagsEx |= DI_FLAGSEX_ALLOWEXCLUDEDDRVS;
    wcsncpy(ip.DriverPath, inf, MAX_PATH - 1);
    ip.DriverPath[MAX_PATH - 1] = 0;
    if (!SetupDiSetDeviceInstallParamsW(h, d, &ip)) {
        *err = GetLastError();
        return -1;
    }
    if (!sb(type == SPDIT_CLASSDRIVER ? "SetupDiBuildDriverInfoList(class)"
                                      : "SetupDiBuildDriverInfoList(compat)",
            SetupDiBuildDriverInfoList(h, d, type))) {
        *err = GetLastError();
        return -1;
    }
    for (i = 0;; i++) {
        cur.cbSize = sizeof cur;
        if (!SetupDiEnumDriverInfoW(h, d, type, i, &cur)) {
            break;
        }
        if (winusb_node(h, d, &cur, hwid, 128)) {
            if (!found || !_wcsicmp(hwid, WINUSB_HWID)) {
                *drv = cur;
                found = 1;
            }
        }
    }
    if (!found) {
        SetupDiDestroyDriverInfoList(h, d, type);
        snote("winusb.inf driver node", "none");
    } else {
        char *t = utf8(drv->Description);
        snote("winusb.inf driver node", t);
        free(t);
    }
    return found;
}

/*
 * The inbox winusb.inf node on the whole device (the camera's route): 1 on
 * WinUSB and running, 0 not. *changed when the device's class or driver
 * was touched.
 */
static int class_bind(const Dev *dev, const Info *before, const char **how,
                      BOOL *reboot, int *changed, Info *after)
{
    DiInstallDevice_fn install =
        (DiInstallDevice_fn)(void (*)(void))newdev_fn("DiInstallDevice");
    wchar_t inf[MAX_PATH + 32];
    UINT wl;
    SP_DEVINFO_DATA d;
    SP_DRVINFO_DATA_W drv;
    HDEVINFO h;
    DWORD err = 0, type = SPDIT_COMPATDRIVER;
    BOOL rb;
    int r;

    *how = "compatible";
    if (!sb("GetProcAddress(DiInstallDevice)", install != NULL)) {
        return 0;
    }
    wl = GetWindowsDirectoryW(inf, MAX_PATH);
    if (wl == 0 || wl >= MAX_PATH) {
        return 0;
    }
    wcscat(inf, L"\\INF\\winusb.inf");
    if (!sb("GetFileAttributesW(winusb.inf)",
            GetFileAttributesW(inf) != INVALID_FILE_ATTRIBUTES)) {
        return 0;
    }
    h = open_dev(dev->inst, &d);
    if (!sb("SetupDiOpenDeviceInfoW", h != INVALID_HANDLE_VALUE)) {
        return 0;
    }

    /* 1: compatible list (devices that report USB\MS_COMP_WINUSB) */
    r = find_winusb(h, &d, SPDIT_COMPATDRIVER, inf, &drv, &err);
    /* 2: class list; matches when the device is already class USBDevice */
    if (r != 1) {
        type = SPDIT_CLASSDRIVER;
        r = find_winusb(h, &d, type, inf, &drv, &err);
        *how = "class";
    }
    /* 3: move the device to class USBDevice, then the class list */
    if (r == 0 && _wcsicmp(before->clsguid, CLS_USBDEVICE) != 0) {
        const wchar_t *g = CLS_USBDEVICE;
        if (before->clsguid[0]) {
            save_class(h, &d, before->clsguid);
        }
        if (sb("SetupDiSetDeviceRegistryPropertyW(CLASSGUID)",
               SetupDiSetDeviceRegistryPropertyW(h, &d, SPDRP_CLASSGUID,
                                                 (const BYTE *)g,
                                                 (DWORD)((wcslen(g) + 1) *
                                                         sizeof(wchar_t))))) {
            *changed = 1;
            r = find_winusb(h, &d, type, inf, &drv, &err);
            *how = "class-guid";
        }
    }

    if (r == 1) {
        if (sb("SetupDiSetSelectedDriverW",
               SetupDiSetSelectedDriverW(h, &d, &drv))) {
            rb = FALSE;
            sstate("state before install", d.DevInst);
            *changed = 1;
            if (sb("DiInstallDevice", install(NULL, h, &d, &drv, 0, &rb)) &&
                rb) {
                snote("DiInstallDevice", "restart requested");
                *reboot = TRUE;
            }
            sstate("state after install", d.DevInst);
        }
        SetupDiDestroyDriverInfoList(h, &d, type);
    }
    SetupDiDestroyDeviceInfoList(h);

    /*
     * DiInstallDevice may return TRUE and install nothing (no Device
     * Install section in setupapi.dev.log): only a running WinUSB devnode
     * with an INF counts.
     */
    {
        /* A composite device takes a moment to start on its new driver */
        int t;
        for (t = 0; t < 40; t++) {
            get_info(dev->inst, after);
            if (after->started && is_winusb(after->service) && after->inf[0]) {
                return 1;
            }
            Sleep(250);
        }
    }
    {
        char t[160], *svc = utf8(after->service), *in = utf8(after->inf);
        snprintf(t, sizeof t, "service '%s' inf '%s' started %d problem %lu",
                 svc, in, after->started, (unsigned long)after->problem);
        snote("check after install", t);
        free(svc);
        free(in);
    }
    return 0;
}

static int cmd_bind(unsigned vid, unsigned pid)
{
    J j;
    Dev dev;
    Info before, after;
    BOOL reboot = FALSE;
    wchar_t now[MAX_DEVICE_ID_LEN];
    int r, changed = 0, storage;
    const char *how = "";

    log_results = 1;
    j_open(&j);
    j_s(&j, "op", "bind");
    j_id(&j, vid, pid);
    if (!is_elevated()) {
        j_b(&j, "ok", 0);
        j_s(&j, "error", "needs administrator: run elevated");
        return finish(&j, EXIT_NOTADMIN);
    }
    r = find_target(vid, pid, &dev, &j, 1);
    if (r) {
        return r;
    }
    {
        char *w = hijack_warnings(&dev, 1);
        j_raw(&j, "warnings", w);
        free(w);
    }
    get_info(dev.inst, &before);
    j_w(&j, "service_before", before.service);
    j_w(&j, "inf_before", before.inf);
    j_w(&j, "class_before", before.cls);
    if (is_winusb(before.service)) {
        j_b(&j, "ok", 1);
        j_b(&j, "already", 1);
        j_info(&j, &before);
        return finish(&j, EXIT_DONE);
    }
    storage = is_storage(dev.inst, &before);
    j_b(&j, "storage", storage);

    /* A held device would need a restart to change driver */
    r = quiesce(dev.dn, &j);
    if (r) {
        return r;
    }

    if (!storage) {
        if (!restart(dev.dn)) {
            DWORD err = 0;
            /* Enumerate it afresh on its own driver */
            if (!remove_dev(dev.inst, NULL, NULL, &reboot, &err, -1) ||
                reboot ||
                !wait_back(dev.inst, vid, pid, &after, now, 30, WANT_ANY)) {
                j_b(&j, "reboot", reboot);
                return fail(&j, "device stopped and did not start again: "
                                "replug it", err);
            }
            wcsncpy(dev.inst, now, MAX_DEVICE_ID_LEN - 1);
            scr("CM_Locate_DevNodeW", CM_Locate_DevNodeW(&dev.dn, dev.inst,
                                                         CM_LOCATE_DEVNODE_NORMAL));
        }
        if (class_bind(&dev, &before, &how, &reboot, &changed, &after)) {
            j_s(&j, "method", how);
            j_info(&j, &after);
            return done(&j, reboot);
        }
        j_s(&j, "class_route", how);
        reboot = FALSE;
    }

    /* Own signed package naming this device */
    if (inf_bind(&dev, &before, &j, &reboot, &after)) {
        j_info(&j, &after);
        return done(&j, reboot);
    }
    reboot = FALSE;
    restore(&j, &dev, &reboot);
    j_b(&j, "reboot", reboot);
    return fail(&j, "the device did not end up on WinUSB", 0);
}

/* ---- unbind ------------------------------------------------------------ */

/* Absent devnodes with this id still bound to WinUSB; each is removed. */
static int remove_absent(unsigned vid, unsigned pid, J *j, BOOL *reboot)
{
    Dev *v;
    int n = enum_usb(0, &v), i, removed = 0;
    Buf list = { 0 };

    if (n < 0) {
        return 0;
    }
    b_str(&list, "[");
    for (i = 0; i < n; i++) {
        SP_DEVINFO_DATA d;
        SP_REMOVEDEVICE_PARAMS rp;
        SP_DEVINSTALL_PARAMS_W ip;
        HDEVINFO h;
        wchar_t svc[64];

        if (v[i].present || v[i].vid != vid || v[i].pid != pid) {
            continue;
        }
        h = open_dev(v[i].inst, &d);
        if (h == INVALID_HANDLE_VALUE) {
            continue;
        }
        dev_prop_into(h, &d, SPDRP_SERVICE, svc, 64);
        if (is_winusb(svc)) {
            memset(&rp, 0, sizeof rp);
            rp.ClassInstallHeader.cbSize = sizeof(SP_CLASSINSTALL_HEADER);
            rp.ClassInstallHeader.InstallFunction = DIF_REMOVE;
            rp.Scope = DI_REMOVEDEVICE_GLOBAL;
            if (SetupDiSetClassInstallParamsW(h, &d, &rp.ClassInstallHeader,
                                              sizeof rp) &&
                SetupDiCallClassInstaller(DIF_REMOVE, h, &d)) {
                char *s = utf8(v[i].inst);
                if (removed++) {
                    b_str(&list, ",");
                }
                b_jstr(&list, s);
                free(s);
                ip.cbSize = sizeof ip;
                if (SetupDiGetDeviceInstallParamsW(h, &d, &ip) &&
                    (ip.Flags & (DI_NEEDREBOOT | DI_NEEDRESTART))) {
                    *reboot = TRUE;
                }
            }
        }
        SetupDiDestroyDeviceInfoList(h);
    }
    b_str(&list, "]");
    j_raw(j, "removed_absent", list.p);
    free(list.p);
    free(v);
    return removed;
}

/* The device back after the rescan, with a driver other than WinUSB. */
static int wait_back(const wchar_t *inst, unsigned vid, unsigned pid,
                     Info *in, wchar_t *now, int seconds, int want)
{
    int t;

    now[0] = 0;
    memset(in, 0, sizeof *in);
    for (t = 0; t < seconds * 2; t++) {
        Dev *v;
        int n = enum_usb(1, &v), i, pick = -1, hits = 0;

        for (i = 0; i < n; i++) {
            if (v[i].vid != vid || v[i].pid != pid) {
                continue;
            }
            hits++;
            if (pick < 0 || !_wcsicmp(v[i].inst, inst)) {
                pick = i;
            }
        }
        if (pick >= 0 && (hits == 1 || !_wcsicmp(v[pick].inst, inst))) {
            wcsncpy(now, v[pick].inst, MAX_DEVICE_ID_LEN - 1);
            now[MAX_DEVICE_ID_LEN - 1] = 0;
            if (get_info(now, in) && in->started && in->service[0] &&
                (want == WANT_ANY ||
                 (want == WANT_WINUSB && is_winusb(in->service)) ||
                 (want == WANT_WINDOWS && !is_userdrv(in)))) {
                free(v);
                return 1;
            }
        }
        free(v);
        Sleep(500);
    }
    return 0;
}

static int cmd_unbind(unsigned vid, unsigned pid)
{
    J j;
    Dev *v, dev;
    Info before, after;
    wchar_t now[MAX_DEVICE_ID_LEN];
    int n, i, hits = 0, r, absent, ours;
    BOOL reboot = FALSE;
    DWORD err = 0;
    long sweep;
    wchar_t name[64];

    log_results = 1;
    j_open(&j);
    j_s(&j, "op", "unbind");
    j_id(&j, vid, pid);
    if (!is_elevated()) {
        j_b(&j, "ok", 0);
        j_s(&j, "error", "needs administrator: run elevated");
        return finish(&j, EXIT_NOTADMIN);
    }
    if (!newdev_fn("DiUninstallDevice")) {
        return fail(&j, "newdev.dll has no DiUninstallDevice",
                    GetLastError());
    }

    n = enum_usb(1, &v);
    for (i = 0; i < n; i++) {
        if (v[i].vid == vid && v[i].pid == pid) {
            hits++;
        }
    }
    free(v);

    if (hits == 0) {
        absent = remove_absent(vid, pid, &j, &reboot);
        absent += sweep_packages(vid, pid, &j);
        if (absent) {
            rescan(0);
            j_b(&j, "present", 0);
            return done(&j, reboot);
        }
        return fail(&j, "no such device present", 0);
    }

    /* Giving back is always allowed */
    r = find_target(vid, pid, &dev, &j, 0);
    if (r) {
        return r;
    }
    {
        char *w = hijack_warnings(&dev, 1);
        j_raw(&j, "warnings", w);
        free(w);
    }
    get_info(dev.inst, &before);
    j_w(&j, "service_before", before.service);
    j_w(&j, "provider_before", before.provider);
    if (!is_userdrv(&before)) {
        j_b(&j, "ok", 0);
        j_s(&j, "refuse", "not on WinUSB");
        return finish(&j, EXIT_REFUSED);
    }
    /* Held open (QEMU still running): refuse, change nothing */
    r = quiesce(dev.dn, &j);
    if (r) {
        return r;
    }
    remove_absent(vid, pid, &j, &reboot);
    ours = !_wcsicmp(before.provider, PROVIDER);
    sweep = (long)(vid << 16 | pid);
    if (!remove_dev(dev.inst, ours ? before.inf : NULL, &j, &reboot, &err,
                    sweep)) {
        restart(dev.dn);
        j_b(&j, "reboot", reboot);
        return fail(&j, "cannot remove device", err);
    }
    cert_name(name, 64, vid, pid);
    cert_remove(name, &j, "certs_removed");
    if (reboot) {
        return done(&j, reboot);
    }
    if (!wait_back(dev.inst, vid, pid, &after, now, 30, WANT_WINDOWS)) {
        /* Still on WinUSB, libusbK, libusb0 or an own package: once more */
        if (now[0] && after.present && is_userdrv(&after)) {
            J p2;
            char *s2;
            int back;
            wchar_t inst2[MAX_DEVICE_ID_LEN];

            wcscpy(inst2, now);
            j_open(&p2);
            j_w(&p2, "service_found", after.service);
            j_w(&p2, "provider_found", after.provider);
            j_w(&p2, "inf_found", after.inf);
            snote("unbind", "second pass");
            back = remove_dev(inst2, NULL, &p2, &reboot, &err, sweep) &&
                   !reboot &&
                   wait_back(inst2, vid, pid, &after, now, 30, WANT_WINDOWS);
            s2 = j_take(&p2);
            j_raw(&j, "second_pass", s2);
            free(s2);
            if (back || reboot) {
                j_w(&j, "instance_after", now);
                j_info(&j, &after);
                return done(&j, reboot);
            }
        }
        if (now[0]) {
            j_w(&j, "instance_after", now);
            j_info(&j, &after);
        }
        j_b(&j, "reboot", reboot);
        return fail(&j, "device did not come back on a Windows driver: "
                        "replug it", 0);
    }
    j_w(&j, "instance_after", now);
    j_info(&j, &after);
    return done(&j, reboot);
}

/* ---- cleanup-cert -------------------------------------------------------- */

/* Remove leftover certificates once no device uses an own package. */
static int cmd_cleanup_cert(void)
{
    J j;
    Dev *v;
    Buf bound = { 0 };
    int n, i, nb = 0;

    log_results = 1;
    j_open(&j);
    j_s(&j, "op", "cleanup-cert");
    if (!is_elevated()) {
        j_b(&j, "ok", 0);
        j_s(&j, "error", "needs administrator: run elevated");
        return finish(&j, EXIT_NOTADMIN);
    }
    n = enum_usb(0, &v);
    b_str(&bound, "[");
    for (i = 0; i < n; i++) {
        Info in;
        if (get_info(v[i].inst, &in) && !_wcsicmp(in.provider, PROVIDER)) {
            char id[16];
            snprintf(id, sizeof id, "%04x:%04x", v[i].vid, v[i].pid);
            b_str(&bound, nb++ ? "," : "");
            b_jstr(&bound, id);
        }
    }
    b_str(&bound, "]");
    free(v);
    if (nb) {
        j_b(&j, "ok", 0);
        j_s(&j, "refuse", "still bound");
        j_raw(&j, "bound", bound.p);
        free(bound.p);
        return finish(&j, EXIT_REFUSED);
    }
    free(bound.p);
    cert_remove(PROVIDER L" ", &j, "certs_removed");
    j_b(&j, "ok", 1);
    return finish(&j, EXIT_DONE);
}

/* ---- log ---------------------------------------------------------------- */

static int print_file(const wchar_t *path)
{
    HANDLE f = CreateFileW(path, GENERIC_READ,
                           FILE_SHARE_READ | FILE_SHARE_WRITE, NULL,
                           OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    char buf[4096];
    DWORD got;

    if (f == INVALID_HANDLE_VALUE) {
        return 0;
    }
    while (ReadFile(f, buf, sizeof buf, &got, NULL) && got) {
        fwrite(buf, 1, got, stdout);
    }
    CloseHandle(f);
    return 1;
}

/* The results of earlier bind, unbind and cleanup-cert runs, oldest first */
static int cmd_log(void)
{
    wchar_t path[MAX_PATH], old[MAX_PATH];

    if (!log_path(path, MAX_PATH, 0)) {
        return EXIT_FAILED;
    }
    wcscpy(old, path);
    wcscpy(old + wcslen(old) - 6, L".1.jsonl");
    print_file(old);
    print_file(path);
    fflush(stdout);
    return EXIT_DONE;
}

/* ---- main -------------------------------------------------------------- */

static int usage(void)
{
    fputs("usage: winusb-switch list [--out FILE]\n"
          "       winusb-switch bind VVVV:PPPP [--out FILE]\n"
          "       winusb-switch unbind VVVV:PPPP [--out FILE]\n"
          "       winusb-switch cleanup-cert [--out FILE]\n"
          "       winusb-switch log\n", stderr);
    return EXIT_USAGE;
}

static int run(int argc, wchar_t **argv)
{
    unsigned vid, pid;

    if (argc == 2 && !wcscmp(argv[1], L"list")) {
        return cmd_list();
    }
    if (argc == 2 && !wcscmp(argv[1], L"cleanup-cert")) {
        return cmd_cleanup_cert();
    }
    if (argc == 2 && !wcscmp(argv[1], L"log")) {
        return cmd_log();
    }
    if (argc != 3 || !parse_id(argv[2], &vid, &pid)) {
        return usage();
    }
    if (!wcscmp(argv[1], L"bind")) {
        return cmd_bind(vid, pid);
    }
    if (!wcscmp(argv[1], L"unbind")) {
        return cmd_unbind(vid, pid);
    }
    return usage();
}

int wmain(int argc, wchar_t **argv)
{
    int r;

    _setmode(_fileno(stdout), _O_BINARY);
    if (argc >= 3 && !wcscmp(argv[argc - 2], L"--out")) {
        out_file = _wfopen(argv[argc - 1], L"wb");
        if (!out_file) {
            fputs("winusb-switch: cannot write the --out file\n", stderr);
            return EXIT_USAGE;
        }
        argc -= 2;
    }
    r = run(argc, argv);
    if (out_file) {
        fclose(out_file);
    }
    return r;
}
