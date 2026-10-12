#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "rtc_correction_event.h"

#define CHECK(x) do { if (!(x)) { fprintf(stderr, "CHECK failed %s:%d: %s\n",     __FILE__, __LINE__, #x); exit(2); } } while (0)

static int g_fail_write;
static int g_fail_sync;
static int g_fail_close;
static int g_fail_clear;
static int g_clear_calls;

size_t __real_fwrite(const void *, size_t, size_t, FILE *);
int __real_fsync(int);
int __real_fclose(FILE *);

size_t __wrap_fwrite(const void *p, size_t s, size_t n, FILE *f) {
    if (g_fail_write) return 0u;
    return __real_fwrite(p, s, n, f);
}
int __wrap_fsync(int fd) {
    if (g_fail_sync) return -1;
    return __real_fsync(fd);
}
int __wrap_fclose(FILE *f) {
    int rc = __real_fclose(f);
    if (g_fail_close) return EOF;
    return rc;
}

static esp_err_t clear_pending(void *ctx) {
    (void)ctx;
    g_clear_calls++;
    return g_fail_clear ? ESP_FAIL : ESP_OK;
}

static const rtc_correction_event_t EVENT = {
    .correction_id = "01234567-89ab-4def-8123-456789abcdef",
    .before = "2026-10-03T00:00:00+00:00",
    .after = "2026-10-03T01:30:00+00:00",
    .source = "pc",
};

static void reset_flags(void) {
    g_fail_write = g_fail_sync = g_fail_close = g_fail_clear = 0;
    g_clear_calls = 0;
}

static void write_text(const char *path, const char *text) {
    FILE *fp = fopen(path, "wb");
    CHECK(fp != NULL);
    CHECK(fwrite(text, 1u, strlen(text), fp) == strlen(text));
    CHECK(fclose(fp) == 0);
}

static char *read_text(const char *path) {
    FILE *fp = fopen(path, "rb");
    long size;
    char *buf;
    CHECK(fp != NULL);
    CHECK(fseek(fp, 0, SEEK_END) == 0);
    size = ftell(fp);
    CHECK(size >= 0);
    CHECK(fseek(fp, 0, SEEK_SET) == 0);
    buf = calloc((size_t)size + 1u, 1u);
    CHECK(buf != NULL);
    CHECK(fread(buf, 1u, (size_t)size, fp) == (size_t)size);
    CHECK(fclose(fp) == 0);
    return buf;
}

static unsigned occurrences(const char *text, const char *needle) {
    unsigned n = 0;
    const char *p = text;
    while ((p = strstr(p, needle)) != NULL) { n++; p += strlen(needle); }
    return n;
}

static void check_one_event(const char *path) {
    char *text = read_text(path);
    CHECK(occurrences(text, "\"event\":\"rtc_correction\"") == 1u);
    CHECK(occurrences(text, EVENT.correction_id) == 1u);
    CHECK(strstr(text, "\"source\":\"pc\"") != NULL);
    free(text);
}

int main(int argc, char **argv) {
    const char *scenario;
    const char *path;
    esp_err_t err;
    char partial[256];

    CHECK(argc == 3);
    scenario = argv[1];
    path = argv[2];
    (void)unlink(path);
    reset_flags();

    if (strcmp(scenario, "success") == 0) {
        CHECK(rtc_correction_recover_event(
                  path, &EVENT, clear_pending, NULL) == ESP_OK);
        CHECK(g_clear_calls == 1);
        check_one_event(path);
    } else if (strcmp(scenario, "clear-crash") == 0) {
        g_fail_clear = 1;
        CHECK(rtc_correction_recover_event(
                  path, &EVENT, clear_pending, NULL) == ESP_FAIL);
        check_one_event(path);
        g_fail_clear = 0;
        CHECK(rtc_correction_recover_event(
                  path, &EVENT, clear_pending, NULL) == ESP_OK);
        CHECK(g_clear_calls == 2);
        check_one_event(path);
    } else if (strcmp(scenario, "torn") == 0) {
        snprintf(partial, sizeof(partial),
                 "{\"event\":\"rtc_correction\",\"correctionId\":\"%s\"",
                 EVENT.correction_id);
        write_text(path, partial);
        CHECK(rtc_correction_recover_event(
                  path, &EVENT, clear_pending, NULL) == ESP_OK);
        CHECK(g_clear_calls == 1);
        check_one_event(path);
    } else if (strcmp(scenario, "substring") == 0) {
        snprintf(partial, sizeof(partial),
                 "{\"event\":\"note\",\"note\":\"%s\"}\n",
                 EVENT.correction_id);
        write_text(path, partial);
        CHECK(rtc_correction_recover_event(
                  path, &EVENT, clear_pending, NULL) == ESP_OK);
        char *text = read_text(path);
        CHECK(occurrences(text, "\"event\":\"rtc_correction\"") == 1u);
        CHECK(occurrences(text, EVENT.correction_id) == 2u);
        free(text);
    } else if (strcmp(scenario, "append-failure") == 0) {
        g_fail_write = 1;
        err = rtc_correction_recover_event(path, &EVENT, clear_pending, NULL);
        CHECK(err == ESP_FAIL && g_clear_calls == 0);
        g_fail_write = 0;
        CHECK(rtc_correction_recover_event(
                  path, &EVENT, clear_pending, NULL) == ESP_OK);
        check_one_event(path);
    } else if (strcmp(scenario, "sync-failure") == 0) {
        g_fail_sync = 1;
        err = rtc_correction_recover_event(path, &EVENT, clear_pending, NULL);
        CHECK(err == ESP_FAIL && g_clear_calls == 0);
        g_fail_sync = 0;
        CHECK(rtc_correction_recover_event(
                  path, &EVENT, clear_pending, NULL) == ESP_OK);
        check_one_event(path);
    } else if (strcmp(scenario, "close-failure") == 0) {
        g_fail_close = 1;
        err = rtc_correction_recover_event(path, &EVENT, clear_pending, NULL);
        CHECK(err == ESP_FAIL && g_clear_calls == 0);
        g_fail_close = 0;
        CHECK(rtc_correction_recover_event(
                  path, &EVENT, clear_pending, NULL) == ESP_OK);
        check_one_event(path);
    } else {
        CHECK(0);
    }

    puts("rtc correction recovery production core: PASS");
    return 0;
}
