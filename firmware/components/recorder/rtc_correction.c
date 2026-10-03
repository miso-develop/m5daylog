// Task #50: M5Capsule v1.1 BM8563 RTC correction + durable NVS intent.
//
// BM8563 is on the Capsule internal I2C bus (SDA GPIO8, SCL GPIO10,
// 7-bit address 0x51). The protocol stores UTC in the RTC and system clock.
// microSD is never accessed by rtc_correction_apply(); the Device-owned
// remount phase calls rtc_correction_flush_pending_event() separately.

#include "rtc_correction.h"

#ifdef ESP_PLATFORM

#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>

#include "device_identity.h"
#include "driver/i2c_master.h"
#include "esp_random.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "recorder_config.h"

#define RTC_I2C_PORT 0
#define RTC_I2C_SDA_PIN 8
#define RTC_I2C_SCL_PIN 10
#define RTC_I2C_ADDR 0x51
#define RTC_I2C_HZ 100000u
#define RTC_I2C_TIMEOUT_MS 100
#define RTC_PENDING_MAGIC 0x52544331u
#define RTC_PENDING_VERSION 1u
#define RTC_NVS_NAMESPACE "m5daylog_rtc"
#define RTC_NVS_PENDING_KEY "pending"
#define RTC_EVENT_LINE_MAX 384u

// BM8563 register map.
#define BM8563_REG_CONTROL1 0x00u
#define BM8563_REG_SECONDS 0x02u
#define BM8563_CONTROL1_STOP 0x20u
#define BM8563_SECONDS_VL 0x80u
#define BM8563_MONTH_CENTURY 0x80u

typedef struct {
    uint32_t magic;
    uint32_t version;
    char correctionId[RECORDER_UUID_STR_LEN];
    char before[RECORDER_ISO8601_STR_LEN];
    char after[RECORDER_ISO8601_STR_LEN];
    char source[4];
} rtc_pending_record_t;

static SemaphoreHandle_t s_rtc_lock = NULL;
static i2c_master_bus_handle_t s_rtc_bus = NULL;
static i2c_master_dev_handle_t s_rtc_dev = NULL;
static bool s_hw_ready = false;
static bool s_pending = false;
static rtc_pending_record_t s_pending_record;

static bool rtc_is_leap(int year) {
    return (year % 4 == 0) && ((year % 100 != 0) || (year % 400 == 0));
}

static int rtc_days_in_month(int year, int month) {
    static const uint8_t days[] = {31, 28, 31, 30, 31, 30,
                                   31, 31, 30, 31, 30, 31};
    if (month < 1 || month > 12) {
        return 0;
    }
    if (month == 2 && rtc_is_leap(year)) {
        return 29;
    }
    return days[month - 1];
}

// Howard Hinnant's civil-date transform, returning days since 1970-01-01.
static int64_t rtc_days_from_civil(int year, unsigned month, unsigned day) {
    int era;
    unsigned yoe;
    unsigned doy;
    unsigned doe;
    year -= month <= 2u;
    era = (year >= 0 ? year : year - 399) / 400;
    yoe = (unsigned)(year - era * 400);
    doy = (153u * (month + (month > 2u ? (unsigned)-3 : 9u)) + 2u) / 5u +
          day - 1u;
    doe = yoe * 365u + yoe / 4u - yoe / 100u + doy;
    return (int64_t)era * 146097LL + (int64_t)doe - 719468LL;
}

static bool rtc_parse_fixed_digits(const char *p, size_t count, int *out) {
    size_t i;
    int value = 0;
    if (p == NULL || out == NULL) {
        return false;
    }
    for (i = 0; i < count; ++i) {
        if (p[i] < '0' || p[i] > '9') {
            return false;
        }
        value = value * 10 + (p[i] - '0');
    }
    *out = value;
    return true;
}

static rtc_correction_result_t rtc_parse_iso8601(const char *text,
                                                  time_t *out_epoch,
                                                  char normalized[RECORDER_ISO8601_STR_LEN]) {
    size_t len;
    size_t pos;
    int year, month, day, hour, minute, second;
    int off_hour = 0;
    int off_minute = 0;
    int off_sign = 0;
    int64_t epoch;
    struct tm utc_tm;
    int n;

    if (text == NULL || out_epoch == NULL || normalized == NULL) {
        return RTC_CORRECTION_INVALID_ARGS;
    }
    len = strlen(text);
    if (len < 20u || len > 40u || text[4] != '-' || text[7] != '-' ||
        text[10] != 'T' || text[13] != ':' || text[16] != ':') {
        return RTC_CORRECTION_INVALID_ARGS;
    }
    if (!rtc_parse_fixed_digits(text + 0, 4, &year) ||
        !rtc_parse_fixed_digits(text + 5, 2, &month) ||
        !rtc_parse_fixed_digits(text + 8, 2, &day) ||
        !rtc_parse_fixed_digits(text + 11, 2, &hour) ||
        !rtc_parse_fixed_digits(text + 14, 2, &minute) ||
        !rtc_parse_fixed_digits(text + 17, 2, &second)) {
        return RTC_CORRECTION_INVALID_ARGS;
    }
    if (month < 1 || month > 12 || day < 1 ||
        day > rtc_days_in_month(year, month) || hour < 0 || hour > 23 ||
        minute < 0 || minute > 59 || second < 0 || second > 59) {
        return RTC_CORRECTION_INVALID_ARGS;
    }

    pos = 19u;
    if (pos < len && text[pos] == '.') {
        size_t fraction_start = ++pos;
        while (pos < len && text[pos] >= '0' && text[pos] <= '9') {
            pos++;
        }
        if (pos == fraction_start) {
            return RTC_CORRECTION_INVALID_ARGS;
        }
    }
    if (pos >= len) {
        return RTC_CORRECTION_INVALID_ARGS;
    }
    if (text[pos] == 'Z') {
        if (pos + 1u != len) {
            return RTC_CORRECTION_INVALID_ARGS;
        }
        off_sign = 0;
    } else if (text[pos] == '+' || text[pos] == '-') {
        off_sign = text[pos] == '+' ? 1 : -1;
        if (pos + 6u != len || text[pos + 3u] != ':' ||
            !rtc_parse_fixed_digits(text + pos + 1u, 2, &off_hour) ||
            !rtc_parse_fixed_digits(text + pos + 4u, 2, &off_minute) ||
            off_hour > 14 || off_minute > 59 ||
            (off_hour == 14 && off_minute != 0)) {
            return RTC_CORRECTION_INVALID_ARGS;
        }
    } else {
        return RTC_CORRECTION_INVALID_ARGS;
    }

    epoch = rtc_days_from_civil(year, (unsigned)month, (unsigned)day) * 86400LL +
            (int64_t)hour * 3600LL + (int64_t)minute * 60LL + second;
    if (off_sign != 0) {
        epoch -= (int64_t)off_sign *
                 ((int64_t)off_hour * 3600LL + (int64_t)off_minute * 60LL);
    }

    // PoC RTC policy: use the unambiguous 2000-2099 BM8563 century window.
    if (epoch < 946684800LL || epoch > 4102444799LL) {
        return RTC_CORRECTION_RANGE_ERROR;
    }
    *out_epoch = (time_t)epoch;
    memset(&utc_tm, 0, sizeof(utc_tm));
    if (gmtime_r(out_epoch, &utc_tm) == NULL) {
        return RTC_CORRECTION_RANGE_ERROR;
    }
    n = snprintf(normalized, RECORDER_ISO8601_STR_LEN,
                 "%04d-%02d-%02dT%02d:%02d:%02d+00:00",
                 utc_tm.tm_year + 1900, utc_tm.tm_mon + 1, utc_tm.tm_mday,
                 utc_tm.tm_hour, utc_tm.tm_min, utc_tm.tm_sec);
    if (n != 25) {
        return RTC_CORRECTION_INTERNAL_ERROR;
    }
    return RTC_CORRECTION_OK;
}

static uint8_t rtc_to_bcd(unsigned value) {
    return (uint8_t)(((value / 10u) << 4u) | (value % 10u));
}

static bool rtc_bcd_valid(uint8_t value) {
    return (value & 0x0fu) <= 9u && ((value >> 4u) & 0x0fu) <= 9u;
}

static unsigned rtc_from_bcd(uint8_t value) {
    return (unsigned)((value >> 4u) & 0x0fu) * 10u + (value & 0x0fu);
}

static esp_err_t rtc_hw_init(void) {
    i2c_master_bus_config_t bus_cfg = {
        .i2c_port = RTC_I2C_PORT,
        .sda_io_num = RTC_I2C_SDA_PIN,
        .scl_io_num = RTC_I2C_SCL_PIN,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true,
    };
    i2c_device_config_t dev_cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = RTC_I2C_ADDR,
        .scl_speed_hz = RTC_I2C_HZ,
    };
    esp_err_t err;

    if (s_hw_ready) {
        return ESP_OK;
    }
    err = i2c_new_master_bus(&bus_cfg, &s_rtc_bus);
    if (err != ESP_OK) {
        return err;
    }
    err = i2c_master_bus_add_device(s_rtc_bus, &dev_cfg, &s_rtc_dev);
    if (err != ESP_OK) {
        i2c_del_master_bus(s_rtc_bus);
        s_rtc_bus = NULL;
        return err;
    }
    s_hw_ready = true;
    return ESP_OK;
}

static esp_err_t rtc_hw_write_epoch(time_t epoch) {
    struct tm utc_tm;
    uint8_t stop[] = {BM8563_REG_CONTROL1, BM8563_CONTROL1_STOP};
    uint8_t start[] = {BM8563_REG_CONTROL1, 0x00u};
    uint8_t payload[8];
    esp_err_t err;

    memset(&utc_tm, 0, sizeof(utc_tm));
    if (gmtime_r(&epoch, &utc_tm) == NULL || utc_tm.tm_year + 1900 < 2000 ||
        utc_tm.tm_year + 1900 > 2099) {
        return ESP_ERR_INVALID_ARG;
    }
    payload[0] = BM8563_REG_SECONDS;
    payload[1] = rtc_to_bcd((unsigned)utc_tm.tm_sec);
    payload[2] = rtc_to_bcd((unsigned)utc_tm.tm_min);
    payload[3] = rtc_to_bcd((unsigned)utc_tm.tm_hour);
    payload[4] = rtc_to_bcd((unsigned)utc_tm.tm_mday);
    payload[5] = rtc_to_bcd((unsigned)utc_tm.tm_wday);
    payload[6] = rtc_to_bcd((unsigned)(utc_tm.tm_mon + 1));  // century=20xx
    payload[7] = rtc_to_bcd((unsigned)((utc_tm.tm_year + 1900) % 100));

    err = i2c_master_transmit(s_rtc_dev, stop, sizeof(stop), RTC_I2C_TIMEOUT_MS);
    if (err != ESP_OK) {
        return err;
    }
    err = i2c_master_transmit(s_rtc_dev, payload, sizeof(payload),
                              RTC_I2C_TIMEOUT_MS);
    if (err != ESP_OK) {
        (void)i2c_master_transmit(s_rtc_dev, start, sizeof(start),
                                  RTC_I2C_TIMEOUT_MS);
        return err;
    }
    return i2c_master_transmit(s_rtc_dev, start, sizeof(start),
                               RTC_I2C_TIMEOUT_MS);
}

static esp_err_t rtc_hw_read_epoch(time_t *out_epoch) {
    uint8_t reg = BM8563_REG_SECONDS;
    uint8_t data[7];
    int year, month, day, hour, minute, second;
    int64_t epoch;
    esp_err_t err;

    if (out_epoch == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    err = i2c_master_transmit_receive(s_rtc_dev, &reg, 1u, data, sizeof(data),
                                      RTC_I2C_TIMEOUT_MS);
    if (err != ESP_OK) {
        return err;
    }
    if ((data[0] & BM8563_SECONDS_VL) != 0u ||
        (data[5] & BM8563_MONTH_CENTURY) != 0u) {
        return ESP_ERR_INVALID_STATE;
    }
    data[0] &= 0x7fu;
    data[1] &= 0x7fu;
    data[2] &= 0x3fu;
    data[3] &= 0x3fu;
    data[4] &= 0x07u;
    data[5] &= 0x1fu;
    if (!rtc_bcd_valid(data[0]) || !rtc_bcd_valid(data[1]) ||
        !rtc_bcd_valid(data[2]) || !rtc_bcd_valid(data[3]) ||
        !rtc_bcd_valid(data[5]) || !rtc_bcd_valid(data[6])) {
        return ESP_ERR_INVALID_STATE;
    }
    second = (int)rtc_from_bcd(data[0]);
    minute = (int)rtc_from_bcd(data[1]);
    hour = (int)rtc_from_bcd(data[2]);
    day = (int)rtc_from_bcd(data[3]);
    month = (int)rtc_from_bcd(data[5]);
    year = 2000 + (int)rtc_from_bcd(data[6]);
    if (month < 1 || month > 12 || day < 1 ||
        day > rtc_days_in_month(year, month) || hour > 23 || minute > 59 ||
        second > 59) {
        return ESP_ERR_INVALID_STATE;
    }
    epoch = rtc_days_from_civil(year, (unsigned)month, (unsigned)day) * 86400LL +
            (int64_t)hour * 3600LL + (int64_t)minute * 60LL + second;
    *out_epoch = (time_t)epoch;
    return ESP_OK;
}

static bool rtc_pending_record_valid(const rtc_pending_record_t *record) {
    size_t source_len;
    if (record == NULL || record->magic != RTC_PENDING_MAGIC ||
        record->version != RTC_PENDING_VERSION ||
        !device_identity_is_valid_uuid(record->correctionId)) {
        return false;
    }
    if (memchr(record->before, '\0', sizeof(record->before)) == NULL ||
        memchr(record->after, '\0', sizeof(record->after)) == NULL ||
        memchr(record->source, '\0', sizeof(record->source)) == NULL) {
        return false;
    }
    source_len = strlen(record->source);
    return source_len == 2u && strcmp(record->source, "pc") == 0 &&
           strlen(record->before) == 25u && strlen(record->after) == 25u;
}

static esp_err_t rtc_load_pending_locked(void) {
    nvs_handle_t handle = 0;
    size_t size = sizeof(s_pending_record);
    rtc_pending_record_t record;
    esp_err_t err;

    memset(&record, 0, sizeof(record));
    err = nvs_open(RTC_NVS_NAMESPACE, NVS_READONLY, &handle);
    if (err == ESP_ERR_NVS_NOT_FOUND) {
        s_pending = false;
        memset(&s_pending_record, 0, sizeof(s_pending_record));
        return ESP_OK;
    }
    if (err != ESP_OK) {
        return err;
    }
    err = nvs_get_blob(handle, RTC_NVS_PENDING_KEY, &record, &size);
    nvs_close(handle);
    if (err == ESP_ERR_NVS_NOT_FOUND) {
        s_pending = false;
        memset(&s_pending_record, 0, sizeof(s_pending_record));
        return ESP_OK;
    }
    if (err != ESP_OK || size != sizeof(record) ||
        !rtc_pending_record_valid(&record)) {
        return ESP_FAIL;
    }
    s_pending_record = record;
    s_pending = true;
    return ESP_OK;
}

static bool rtc_make_correction_id(char out[RECORDER_UUID_STR_LEN]) {
    uint8_t bytes[16];
    unsigned i;
    for (i = 0; i < 4u; ++i) {
        uint32_t word = esp_random();
        bytes[i * 4u + 0u] = (uint8_t)(word >> 24u);
        bytes[i * 4u + 1u] = (uint8_t)(word >> 16u);
        bytes[i * 4u + 2u] = (uint8_t)(word >> 8u);
        bytes[i * 4u + 3u] = (uint8_t)word;
    }
    return device_identity_format_uuid_v4(bytes, out);
}

static void rtc_format_epoch(time_t epoch,
                             char out[RECORDER_ISO8601_STR_LEN]) {
    struct tm utc_tm;
    memset(out, 0, RECORDER_ISO8601_STR_LEN);
    memset(&utc_tm, 0, sizeof(utc_tm));
    if (gmtime_r(&epoch, &utc_tm) == NULL ||
        snprintf(out, RECORDER_ISO8601_STR_LEN,
                 "%04d-%02d-%02dT%02d:%02d:%02d+00:00",
                 utc_tm.tm_year + 1900, utc_tm.tm_mon + 1, utc_tm.tm_mday,
                 utc_tm.tm_hour, utc_tm.tm_min, utc_tm.tm_sec) != 25) {
        memcpy(out, "1970-01-01T00:00:00+00:00", 26u);
    }
}

static bool rtc_correction_event_already_recorded(const char *events_path,
                                                   const char *correction_id) {
    FILE *fp;
    char needle[80];
    char line[RTC_EVENT_LINE_MAX];
    if (events_path == NULL || correction_id == NULL ||
        snprintf(needle, sizeof(needle), "\"correctionId\":\"%s\"",
                 correction_id) <= 0) {
        return false;
    }
    fp = fopen(events_path, "rb");
    if (fp == NULL) {
        return false;
    }
    while (fgets(line, sizeof(line), fp) != NULL) {
        if (strstr(line, needle) != NULL) {
            fclose(fp);
            return true;
        }
    }
    fclose(fp);
    return false;
}

esp_err_t rtc_correction_init(void) {
    esp_err_t hw_err;
    esp_err_t nvs_err;
    time_t rtc_epoch;

    if (s_rtc_lock == NULL) {
        s_rtc_lock = xSemaphoreCreateMutex();
        if (s_rtc_lock == NULL) {
            return ESP_ERR_NO_MEM;
        }
    }
    if (xSemaphoreTake(s_rtc_lock, portMAX_DELAY) != pdTRUE) {
        return ESP_FAIL;
    }
    hw_err = rtc_hw_init();
    if (hw_err == ESP_OK && rtc_hw_read_epoch(&rtc_epoch) == ESP_OK) {
        struct timeval tv = {.tv_sec = rtc_epoch, .tv_usec = 0};
        (void)settimeofday(&tv, NULL);
    }
    nvs_err = nvs_flash_init();
    if (nvs_err == ESP_OK) {
        nvs_err = rtc_load_pending_locked();
    }
    xSemaphoreGive(s_rtc_lock);
    // An invalid/unset RTC is recoverable by SET_TIME; transport failure is not.
    if (hw_err != ESP_OK) {
        return hw_err;
    }
    return nvs_err;
}

bool rtc_correction_is_pending(void) {
    bool pending = true;
    if (s_rtc_lock == NULL ||
        xSemaphoreTake(s_rtc_lock, portMAX_DELAY) != pdTRUE) {
        return true;  // fail closed: do not permit an untracked mutation
    }
    pending = s_pending;
    xSemaphoreGive(s_rtc_lock);
    return pending;
}

rtc_correction_result_t rtc_correction_apply(
    const char *requested_time,
    char *normalized,
    size_t normalized_size) {
    char normalized_local[RECORDER_ISO8601_STR_LEN];
    rtc_pending_record_t candidate;
    rtc_correction_result_t parse_result;
    struct timeval before_tv;
    struct timeval after_tv;
    time_t requested_epoch;
    nvs_handle_t handle = 0;
    esp_err_t err;

    if (normalized == NULL || normalized_size < RECORDER_ISO8601_STR_LEN) {
        return RTC_CORRECTION_INVALID_ARGS;
    }
    normalized[0] = '\0';
    memset(normalized_local, 0, sizeof(normalized_local));
    parse_result = rtc_parse_iso8601(requested_time, &requested_epoch,
                                     normalized_local);
    if (parse_result != RTC_CORRECTION_OK) {
        return parse_result;
    }
    if (s_rtc_lock == NULL ||
        xSemaphoreTake(s_rtc_lock, portMAX_DELAY) != pdTRUE) {
        return RTC_CORRECTION_INTERNAL_ERROR;
    }
    if (s_pending) {
        xSemaphoreGive(s_rtc_lock);
        return RTC_CORRECTION_BUSY;
    }
    if (!s_hw_ready) {
        xSemaphoreGive(s_rtc_lock);
        return RTC_CORRECTION_INTERNAL_ERROR;
    }

    memset(&candidate, 0, sizeof(candidate));
    candidate.magic = RTC_PENDING_MAGIC;
    candidate.version = RTC_PENDING_VERSION;
    memcpy(candidate.source, "pc", 3u);
    if (!rtc_make_correction_id(candidate.correctionId)) {
        xSemaphoreGive(s_rtc_lock);
        return RTC_CORRECTION_INTERNAL_ERROR;
    }
    if (gettimeofday(&before_tv, NULL) != 0) {
        before_tv.tv_sec = 0;
        before_tv.tv_usec = 0;
    }
    rtc_format_epoch(before_tv.tv_sec, candidate.before);
    memcpy(candidate.after, normalized_local, sizeof(candidate.after));

    // Mutation begins only after the request has been fully validated.
    err = rtc_hw_write_epoch(requested_epoch);
    if (err != ESP_OK) {
        xSemaphoreGive(s_rtc_lock);
        return RTC_CORRECTION_INTERNAL_ERROR;
    }
    after_tv.tv_sec = requested_epoch;
    after_tv.tv_usec = 0;
    if (settimeofday(&after_tv, NULL) != 0) {
        xSemaphoreGive(s_rtc_lock);
        return RTC_CORRECTION_INTERNAL_ERROR;
    }

    // Hardware time is already changed at this point. Any failure below is
    // deliberately INTERNAL_ERROR/indeterminate; callers must not retry blindly.
    err = nvs_open(RTC_NVS_NAMESPACE, NVS_READWRITE, &handle);
    if (err == ESP_OK) {
        err = nvs_set_blob(handle, RTC_NVS_PENDING_KEY, &candidate,
                           sizeof(candidate));
    }
    if (err == ESP_OK) {
        err = nvs_commit(handle);
    }
    if (handle != 0) {
        nvs_close(handle);
    }
    if (err != ESP_OK) {
        xSemaphoreGive(s_rtc_lock);
        return RTC_CORRECTION_INTERNAL_ERROR;
    }
    s_pending_record = candidate;
    s_pending = true;
    memcpy(normalized, normalized_local, RECORDER_ISO8601_STR_LEN);
    xSemaphoreGive(s_rtc_lock);
    return RTC_CORRECTION_OK;
}

esp_err_t rtc_correction_flush_pending_event(const char *events_path) {
    rtc_pending_record_t record;
    bool already_recorded;
    FILE *fp = NULL;
    char line[RTC_EVENT_LINE_MAX];
    int line_len;
    nvs_handle_t handle = 0;
    esp_err_t err = ESP_OK;

    if (events_path == NULL || events_path[0] == '\0' || s_rtc_lock == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    if (xSemaphoreTake(s_rtc_lock, portMAX_DELAY) != pdTRUE) {
        return ESP_FAIL;
    }
    if (!s_pending) {
        xSemaphoreGive(s_rtc_lock);
        return ESP_OK;
    }
    record = s_pending_record;
    already_recorded = rtc_correction_event_already_recorded(
        events_path, record.correctionId);
    if (!already_recorded) {
        line_len = snprintf(
            line, sizeof(line),
            "{\"event\":\"rtc_correction\",\"correctionId\":\"%s\","
            "\"before\":\"%s\",\"after\":\"%s\",\"source\":\"pc\"}\n",
            record.correctionId, record.before, record.after);
        if (line_len <= 0 || (size_t)line_len >= sizeof(line)) {
            xSemaphoreGive(s_rtc_lock);
            return ESP_FAIL;
        }
        fp = fopen(events_path, "ab");
        if (fp == NULL || fwrite(line, 1u, (size_t)line_len, fp) !=
                              (size_t)line_len ||
            fflush(fp) != 0 || fsync(fileno(fp)) != 0) {
            if (fp != NULL) {
                fclose(fp);
            }
            xSemaphoreGive(s_rtc_lock);
            return ESP_FAIL;
        }
        if (fclose(fp) != 0) {
            xSemaphoreGive(s_rtc_lock);
            return ESP_FAIL;
        }
    }

    // Clear intent only after the event is known durable (or found already
    // durable after a crash between fsync and this NVS commit).
    err = nvs_open(RTC_NVS_NAMESPACE, NVS_READWRITE, &handle);
    if (err == ESP_OK) {
        err = nvs_erase_key(handle, RTC_NVS_PENDING_KEY);
        if (err == ESP_ERR_NVS_NOT_FOUND) {
            err = ESP_OK;
        }
    }
    if (err == ESP_OK) {
        err = nvs_commit(handle);
    }
    if (handle != 0) {
        nvs_close(handle);
    }
    if (err == ESP_OK) {
        s_pending = false;
        memset(&s_pending_record, 0, sizeof(s_pending_record));
    }
    xSemaphoreGive(s_rtc_lock);
    return err;
}

#else  // !ESP_PLATFORM

esp_err_t rtc_correction_init(void) {
    return ESP_ERR_NOT_SUPPORTED;
}

bool rtc_correction_is_pending(void) {
    return false;
}

rtc_correction_result_t rtc_correction_apply(
    const char *requested_time, char *normalized, size_t normalized_size) {
    (void)requested_time;
    if (normalized != NULL && normalized_size != 0u) {
        normalized[0] = '\0';
    }
    return RTC_CORRECTION_INTERNAL_ERROR;
}

esp_err_t rtc_correction_flush_pending_event(const char *events_path) {
    (void)events_path;
    return ESP_ERR_NOT_SUPPORTED;
}

#endif
