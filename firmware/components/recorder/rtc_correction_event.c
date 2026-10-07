// Task #50: durable exactly-once rtc_correction JSONL recovery.

#include "rtc_correction_event.h"

#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <sys/types.h>
#include <unistd.h>

#include "cJSON.h"

#define RTC_EVENT_LINE_MAX 512u

static bool json_string_equals(const cJSON *root, const char *key,
                               const char *expected) {
    cJSON *item;
    if (root == NULL || key == NULL || expected == NULL) return false;
    item = cJSON_GetObjectItemCaseSensitive(root, key);
    return cJSON_IsString(item) && item->valuestring != NULL &&
           strcmp(item->valuestring, expected) == 0;
}

static esp_err_t classify_complete_event_line(
    const char *line,
    const rtc_correction_event_t *event,
    bool *matching) {
    cJSON *root;
    cJSON *id_item;

    if (line == NULL || event == NULL || matching == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    *matching = false;
    root = cJSON_ParseWithOpts(line, NULL, true);
    if (root == NULL || !cJSON_IsObject(root)) {
        cJSON_Delete(root);
        return ESP_FAIL;
    }

    id_item = cJSON_GetObjectItemCaseSensitive(root, "correctionId");
    if (cJSON_IsString(id_item) && id_item->valuestring != NULL &&
        strcmp(id_item->valuestring, event->correction_id) == 0 &&
        json_string_equals(root, "event", "rtc_correction")) {
        // The correctionId is the idempotency key. A conflicting payload with
        // the same key is not accepted as success.
        if (!json_string_equals(root, "before", event->before) ||
            !json_string_equals(root, "after", event->after) ||
            !json_string_equals(root, "source", event->source)) {
            cJSON_Delete(root);
            return ESP_FAIL;
        }
        *matching = true;
    }

    cJSON_Delete(root);
    return ESP_OK;
}

esp_err_t rtc_correction_recover_event(
    const char *events_path,
    const rtc_correction_event_t *event,
    rtc_correction_pending_clear_fn_t clear_pending,
    void *clear_ctx) {
    FILE *fp;
    char line[RTC_EVENT_LINE_MAX];
    char out[RTC_EVENT_LINE_MAX];
    long last_complete = 0;
    bool found = false;
    int out_len;

    if (events_path == NULL || events_path[0] == '\0' || event == NULL ||
        event->correction_id == NULL || event->before == NULL ||
        event->after == NULL || event->source == NULL ||
        strcmp(event->source, "pc") != 0 || clear_pending == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    fp = fopen(events_path, "r+b");
    if (fp == NULL && errno == ENOENT) {
        fp = fopen(events_path, "w+b");
    }
    if (fp == NULL) {
        return ESP_FAIL;
    }

    while (fgets(line, sizeof(line), fp) != NULL) {
        size_t len = strlen(line);
        bool matching = false;
        long after;

        if (len == 0u) {
            if (fclose(fp) != 0) return ESP_FAIL;
            return ESP_FAIL;
        }

        if (line[len - 1u] != '\n') {
            if (!feof(fp)) {
                // A complete line larger than our bounded recovery buffer is
                // not safe to reinterpret or truncate.
                (void)fclose(fp);
                return ESP_FAIL;
            }

            // Incomplete EOF tail is never treated as an existing success.
            // Remove only that tail, preserving all complete prior JSONL
            // records, then append the authoritative complete event below.
            clearerr(fp);
            if (ftruncate(fileno(fp), (off_t)last_complete) != 0 ||
                fseek(fp, last_complete, SEEK_SET) != 0 ||
                fsync(fileno(fp)) != 0) {
                (void)fclose(fp);
                return ESP_FAIL;
            }
            break;
        }

        if (classify_complete_event_line(line, event, &matching) != ESP_OK) {
            (void)fclose(fp);
            return ESP_FAIL;
        }
        if (matching) {
            found = true;
        }

        after = ftell(fp);
        if (after < 0) {
            (void)fclose(fp);
            return ESP_FAIL;
        }
        last_complete = after;
    }

    if (ferror(fp)) {
        (void)fclose(fp);
        return ESP_FAIL;
    }

    if (!found) {
        if (fseek(fp, 0, SEEK_END) != 0) {
            (void)fclose(fp);
            return ESP_FAIL;
        }
        out_len = snprintf(
            out, sizeof(out),
            "{\"event\":\"rtc_correction\",\"correctionId\":\"%s\","
            "\"before\":\"%s\",\"after\":\"%s\",\"source\":\"pc\"}\n",
            event->correction_id, event->before, event->after);
        if (out_len <= 0 || (size_t)out_len >= sizeof(out) ||
            fwrite(out, 1u, (size_t)out_len, fp) != (size_t)out_len ||
            fflush(fp) != 0 || fsync(fileno(fp)) != 0) {
            (void)fclose(fp);
            return ESP_FAIL;
        }
    }

    // fclose is part of the durable-file completion boundary. A close failure
    // leaves pending set; a later recovery will re-scan and avoid duplication.
    if (fclose(fp) != 0) {
        return ESP_FAIL;
    }

    return clear_pending(clear_ctx);
}
