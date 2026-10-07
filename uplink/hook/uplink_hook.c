/* Claude Code hook of DedSec Uplink: appends one line per event to
 * %LOCALAPPDATA%\DedSecUplink\claude_events.jsonl, the same record as hooks.record_event():
 *   {"ts": <unix seconds>, "session_id": "...", "event": "...", "message": "...", "cwd": "..."}
 * Native, so a hook costs milliseconds instead of starting the 30 MB one-file companion (about 2 s,
 * which Claude waits for on every prompt). It never fails visibly and never blocks Claude.
 *
 * Build (MSVC):  build.bat   ->  uplink_hook.exe (no console window, static CRT)
 */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdio.h>
#include <string.h>

#define MAX_INPUT   (256 * 1024)
#define MAX_FILE    (256 * 1024)
#define KEEP_LINES  500
#define MESSAGE_MAX 200 /* characters, as in hooks.record_event() */

static char input[MAX_INPUT + 1];
static char line[8192];

/* Raw content of the string value of "key" (escapes kept, so it can be copied into JSON as is). */
static const char* find_string(const char* json, const char* key, size_t* len) {
    char pattern[48];
    snprintf(pattern, sizeof(pattern), "\"%s\"", key);
    for(const char* p = strstr(json, pattern); p; p = strstr(p + 1, pattern)) {
        const char* q = p + strlen(pattern);
        while(*q == ' ' || *q == '\t' || *q == '\r' || *q == '\n') q++;
        if(*q != ':') continue;
        q++;
        while(*q == ' ' || *q == '\t' || *q == '\r' || *q == '\n') q++;
        if(*q != '"') return NULL;
        const char* start = ++q;
        while(*q && *q != '"') q += (*q == '\\' && q[1]) ? 2 : 1;
        if(*q != '"') return NULL;
        *len = (size_t)(q - start);
        return start;
    }
    return NULL;
}

/* Length in bytes of the first `chars` characters of a raw JSON string (an escape sequence or a
 * UTF-8 code point counts as one), so a cut never splits either. */
static size_t cut_chars(const char* s, size_t len, size_t chars) {
    size_t i = 0;
    while(i < len && chars--) {
        unsigned char c = (unsigned char)s[i];
        if(c == '\\') {
            i += (i + 1 < len && s[i + 1] == 'u') ? 6 : 2;
        } else if(c >= 0xF0) {
            i += 4;
        } else if(c >= 0xE0) {
            i += 3;
        } else if(c >= 0xC0) {
            i += 2;
        } else {
            i += 1;
        }
    }
    return i > len ? len : i;
}

static size_t put_field(char* out, size_t used, size_t size, const char* name, const char* json,
                        size_t max_chars) {
    size_t len = 0;
    const char* value = find_string(json, name, &len);
    if(!value) len = 0;
    if(max_chars) len = cut_chars(value ? value : "", len, max_chars);
    const char* label = name;
    if(strcmp(name, "hook_event_name") == 0) label = "event";
    int n = snprintf(out + used, size - used, ", \"%s\": \"%.*s\"", label, (int)len, value ? value : "");
    if(n < 0 || (size_t)n >= size - used) return used;
    return used + (size_t)n;
}

static void trim_file(const wchar_t* path) {
    HANDLE h = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                           NULL, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    if(h == INVALID_HANDLE_VALUE) return;
    LARGE_INTEGER size;
    if(!GetFileSizeEx(h, &size) || size.QuadPart <= MAX_FILE || size.QuadPart > 64 * MAX_FILE) {
        CloseHandle(h);
        return;
    }
    DWORD total = (DWORD)size.QuadPart, got = 0;
    char* data = HeapAlloc(GetProcessHeap(), 0, total);
    if(!data || !ReadFile(h, data, total, &got, NULL)) got = 0;
    CloseHandle(h);
    if(!got) return;
    DWORD start = got, lines = 0;
    while(start > 0 && lines <= KEEP_LINES) {
        start--;
        if(data[start] == '\n' && start + 1 < got) lines++;
    }
    if(lines > KEEP_LINES) start++;
    h = CreateFileW(path, GENERIC_WRITE, FILE_SHARE_READ, NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if(h != INVALID_HANDLE_VALUE) {
        DWORD written;
        WriteFile(h, data + start, got - start, &written, NULL);
        CloseHandle(h);
    }
    HeapFree(GetProcessHeap(), 0, data);
}

int main(void) {
    HANDLE in = GetStdHandle(STD_INPUT_HANDLE);
    DWORD got = 0, total = 0;
    while(in && in != INVALID_HANDLE_VALUE && total < MAX_INPUT &&
          ReadFile(in, input + total, MAX_INPUT - total, &got, NULL) && got)
        total += got;
    input[total] = 0;

    FILETIME ft;
    GetSystemTimeAsFileTime(&ft);
    ULONGLONG t = ((ULONGLONG)ft.dwHighDateTime << 32) | ft.dwLowDateTime;
    t -= 116444736000000000ULL; /* 1601 -> 1970, in 100 ns */
    size_t used = (size_t)snprintf(line, sizeof(line), "{\"ts\": %llu.%06llu", t / 10000000ULL,
                                   (t % 10000000ULL) / 10ULL);
    used = put_field(line, used, sizeof(line), "session_id", input, 0);
    used = put_field(line, used, sizeof(line), "hook_event_name", input, 0);
    used = put_field(line, used, sizeof(line), "message", input, MESSAGE_MAX);
    used = put_field(line, used, sizeof(line), "cwd", input, 0);
    if(used + 3 > sizeof(line)) return 0;
    line[used++] = '}';
    line[used++] = '\n';

    wchar_t dir[MAX_PATH], path[MAX_PATH];
    DWORD n = GetEnvironmentVariableW(L"LOCALAPPDATA", dir, MAX_PATH);
    if(!n || n >= MAX_PATH - 40) return 0;
    wcscat_s(dir, MAX_PATH, L"\\DedSecUplink");
    CreateDirectoryW(dir, NULL);
    swprintf_s(path, MAX_PATH, L"%s\\claude_events.jsonl", dir);
    trim_file(path);
    HANDLE out = CreateFileW(path, FILE_APPEND_DATA, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                             NULL, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if(out != INVALID_HANDLE_VALUE) {
        DWORD written;
        WriteFile(out, line, (DWORD)used, &written, NULL);
        CloseHandle(out);
    }
    return 0;
}
