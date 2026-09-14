// Portable RIFF/WAVE `.wav.part` framing — Task #44. Stdio only.
//
// Durability note (Human Gate zero-byte fix): stdio fflush() alone leaves
// checkpoint bytes in FatFs/VFS caches; on ESP-IDF every explicit
// checkpoint (flush/close) and the initial placeholder header additionally
// issue fsync(fileno()) so the FatFs f_sync path reaches the media.
// Sync failure is fail-loud (false) so the recorder path counts sd_err,
// requests STOP, and never reports a successful checkpoint.

#include "wav_part.h"

#include <string.h>

#ifdef ESP_PLATFORM
#include <unistd.h>
#endif

static void put_u16le(uint8_t *dst, uint16_t value) {
    dst[0] = (uint8_t)(value & 0xFFu);
    dst[1] = (uint8_t)((value >> 8) & 0xFFu);
}

static void put_u32le(uint8_t *dst, uint32_t value) {
    dst[0] = (uint8_t)(value & 0xFFu);
    dst[1] = (uint8_t)((value >> 8) & 0xFFu);
    dst[2] = (uint8_t)((value >> 16) & 0xFFu);
    dst[3] = (uint8_t)((value >> 24) & 0xFFu);
}

static bool has_part_suffix(const char *path) {
    if (path == NULL) {
        return false;
    }
    size_t len = strlen(path);
    size_t suffix = strlen(RECORDER_PART_SUFFIX);
    if (len < suffix) {
        return false;
    }
    return strcmp(path + len - suffix, RECORDER_PART_SUFFIX) == 0;
}

void wav_part_build_header(uint8_t header[RECORDER_WAV_HEADER_SIZE],
                           uint32_t pcm_bytes, uint32_t sample_rate_hz,
                           uint16_t channels, uint16_t bits_per_sample) {
    uint16_t bytes_per_sample = (uint16_t)(bits_per_sample / 8u);
    uint16_t block_align = (uint16_t)(channels * bytes_per_sample);
    uint32_t byte_rate = sample_rate_hz * (uint32_t)block_align;

    memcpy(header + 0, "RIFF", 4);
    put_u32le(header + 4, 36u + pcm_bytes);
    memcpy(header + 8, "WAVE", 4);
    memcpy(header + 12, "fmt ", 4);
    put_u32le(header + 16, 16u);  // PCM fmt chunk size
    put_u16le(header + 20, 1u);   // AudioFormat = PCM
    put_u16le(header + 22, channels);
    put_u32le(header + 24, sample_rate_hz);
    put_u32le(header + 28, byte_rate);
    put_u16le(header + 32, block_align);
    put_u16le(header + 34, bits_per_sample);
    memcpy(header + 36, "data", 4);
    put_u32le(header + 40, pcm_bytes);
}

static bool wav_part_sync_media(FILE *fp) {
    if (fp == NULL) {
        return false;
    }
    if (fflush(fp) != 0) {
        return false;
    }
#ifdef ESP_PLATFORM
    // Durable checkpoint: push VFS/FatFs caches to the media via the
    // FatFs f_sync path. fileno/fsync failure is fail-loud.
    {
        int fd = fileno(fp);
        if (fd < 0) {
            return false;
        }
        if (fsync(fd) != 0) {
            return false;
        }
    }
#endif
    return true;
}

static bool wav_part_rewrite_header(wav_part_t *part) {
    uint8_t header[RECORDER_WAV_HEADER_SIZE];
    long tail;

    wav_part_build_header(header, part->pcm_bytes, part->sample_rate_hz,
                          part->channels, part->bits_per_sample);
    if (fseek(part->fp, 0L, SEEK_SET) != 0) {
        return false;
    }
    if (fwrite(header, 1, sizeof(header), part->fp) != sizeof(header)) {
        return false;
    }
    if (!wav_part_sync_media(part->fp)) {
        return false;
    }
    // Return to the append position so the next write continues the payload.
    tail = (long)(RECORDER_WAV_HEADER_SIZE + part->pcm_bytes);
    if (fseek(part->fp, tail, SEEK_SET) != 0) {
        return false;
    }
    return true;
}

bool wav_part_open(wav_part_t *part, const char *path,
                   uint32_t sample_rate_hz, uint16_t channels,
                   uint16_t bits_per_sample) {
    uint8_t header[RECORDER_WAV_HEADER_SIZE];

    if (part == NULL || !has_part_suffix(path)) {
        return false;
    }
    if (channels == 0 || bits_per_sample == 0 ||
        (bits_per_sample % 8u) != 0) {
        return false;
    }
    part->fp = fopen(path, "wb");
    if (part->fp == NULL) {
        part->open = false;
        return false;
    }
    part->pcm_bytes = 0;
    part->sample_rate_hz = sample_rate_hz;
    part->channels = channels;
    part->bits_per_sample = bits_per_sample;
    part->open = true;

    wav_part_build_header(header, 0, sample_rate_hz, channels,
                          bits_per_sample);
    if (fwrite(header, 1, sizeof(header), part->fp) != sizeof(header)) {
        fclose(part->fp);
        part->fp = NULL;
        part->open = false;
        part->pcm_bytes = 0;
        return false;
    }
    if (!wav_part_sync_media(part->fp)) {
        // A placeholder header that never reached durable storage must not
        // leave an open FILE behind: close, clear state, fail-loud.
        fclose(part->fp);
        part->fp = NULL;
        part->open = false;
        part->pcm_bytes = 0;
        return false;
    }
    return true;
}

bool wav_part_write(wav_part_t *part, const uint8_t *pcm, size_t len) {
    size_t usable;

    if (part == NULL || !part->open || part->fp == NULL) {
        return false;
    }
    if (pcm == NULL || len == 0) {
        return true;
    }
    // Sample-boundary rule (Spec #36): truncate only a misaligned tail.
    usable = len - (len % (size_t)(part->bits_per_sample / 8u));
    if (usable == 0) {
        return true;
    }
    if (fwrite(pcm, 1, usable, part->fp) != usable) {
        return false;
    }
    part->pcm_bytes += (uint32_t)usable;
    return true;
}

bool wav_part_flush(wav_part_t *part) {
    if (part == NULL || !part->open || part->fp == NULL) {
        return false;
    }
    return wav_part_rewrite_header(part);
}

bool wav_part_close(wav_part_t *part) {
    bool ok = true;

    if (part == NULL || !part->open) {
        return true;
    }
    if (part->fp != NULL) {
        ok = wav_part_rewrite_header(part);
        if (fclose(part->fp) != 0) {
            ok = false;
        }
    }
    part->fp = NULL;
    part->open = false;
    return ok;
}

uint32_t wav_part_pcm_bytes(const wav_part_t *part) {
    if (part == NULL) {
        return 0;
    }
    return part->pcm_bytes;
}
