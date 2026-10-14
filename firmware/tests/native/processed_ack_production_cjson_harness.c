// Real ESP-IDF cJSON regression: escaped JSON NUL must never apply a raw ID.
// Run by firmware CI against the same cJSON.c shipped with ESP-IDF v5.5.5.
// All I/O is a temporary sandbox on the CI host, never a physical device.
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include "processed_ack.h"

#define REC_ID "11111111-1111-4111-8111-111111111111"
#define DEV_ID "33333333-3333-4333-8333-333333333333"
#define MARKER "processed-state/" REC_ID ".applied"

static const char *valid_manifest =
    "{\"schemaVersion\":1,\"deviceId\":\"" DEV_ID "\","
    "\"updatedAt\":\"2026-10-11T13:00:00Z\",\"recordings\":[{"
    "\"recordingId\":\"" REC_ID "\","
    "\"filename\":\"recordings/2026-10-11/120000_" REC_ID ".wav\","
    "\"startedAt\":\"2026-10-11T12:00:00Z\","
    "\"durationMs\":1000,\"sizeBytes\":32044,"
    "\"sha256\":\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
                 "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\","
    "\"sampleRate\":16000,\"bitDepth\":16,\"channels\":1,"
    "\"state\":\"finalized\",\"firmwareVersion\":\"0.1.0\"}]}";

static const char *valid_ack =
    "{\"schemaVersion\":1,\"deviceId\":\"" DEV_ID "\","
    "\"processedAt\":\"2026-10-12T12:00:00Z\","
    "\"recordingIds\":[\"" REC_ID "\"]}";

static const char *invalid_ack =
    "{\"schemaVersion\":1,\"deviceId\":\"" DEV_ID "\","
    "\"processedAt\":\"2026-10-12T12:00:00Z\","
    "\"recordingIds\":[\"" REC_ID "\\u0000not-a-uuid\"]}";

static bool write_doc(const char *path, const char *doc) {
    FILE *fp = fopen(path, "wb");
    size_t len = strlen(doc);
    if (fp == NULL) return false;
    if (fwrite(doc, 1u, len, fp) != len) {
        (void)fclose(fp);
        return false;
    }
    return fclose(fp) == 0;
}

int main(void) {
    char dir[] = "/tmp/m5daylog-cjson-XXXXXX";
    processed_ack_stats_t s;
    if (mkdtemp(dir) == NULL || chdir(dir) != 0 ||
        mkdir("acks", 0700) != 0 ||
        mkdir("processed-state", 0700) != 0) {
        fputs("sandbox setup failed\n", stderr);
        return 1;
    }
    // Malformed ACK with an otherwise matching prefix must apply nothing.
    if (!write_doc("manifest.json", valid_manifest) ||
        !write_doc("acks/processed.json", invalid_ack) ||
        !processed_ack_apply_manual_wake(DEV_ID, &s) ||
        s.invalid_payloads != 1u || s.applied != 0u ||
        access(MARKER, F_OK) == 0) {
        fputs("FAIL: decoded NUL ACK applied under real cJSON\n", stderr);
        return 2;
    }
    // Manifest IDs are also untrusted and must not be prefix-validated.
    {
        char bad_manifest[1024];
        const char *needle = "\"recordingId\":\"" REC_ID "\"";
        const char *at = strstr(valid_manifest, needle);
        size_t front;
        if (at == NULL) return 3;
        front = (size_t)(at - valid_manifest);
        if (front >= sizeof(bad_manifest)) return 3;
        memcpy(bad_manifest, valid_manifest, front);
        bad_manifest[front] = '\0';
        if (snprintf(bad_manifest + front, sizeof(bad_manifest) - front,
                     "\"recordingId\":\"" REC_ID "\\u0000foreign\"%s",
                     at + strlen(needle)) >=
            (int)(sizeof(bad_manifest) - front)) return 3;
        if (!write_doc("manifest.json", bad_manifest) ||
            !write_doc("acks/processed.json", valid_ack) ||
            processed_ack_apply_manual_wake(DEV_ID, &s) ||
            s.applied != 0u || access(MARKER, F_OK) == 0) {
            fputs("FAIL: decoded NUL manifest applied under real cJSON\n", stderr);
            return 4;
        }
    }
    // Ensure valid ordinary payloads are still applied exactly once.
    if (!write_doc("manifest.json", valid_manifest) ||
        !processed_ack_apply_manual_wake(DEV_ID, &s) ||
        s.applied != 1u || access(MARKER, F_OK) != 0 ||
        !processed_ack_apply_manual_wake(DEV_ID, &s) ||
        s.applied != 0u || s.already_applied != 1u) {
        fputs("FAIL: valid ACK apply or replay\n", stderr);
        return 5;
    }
    puts("PASS: production cJSON escaped-NUL rejection and valid replay");
    return 0;
}
