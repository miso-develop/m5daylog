// Portable power-loss recovery / quarantine — Task #46 (IM-009).
// Stdio + dirent + sys/stat + unistd only; no ESP-IDF dependency.
//
// Durability: header rewrites use fflush() plus fsync(fileno()) so the
// rebuilt header reaches the media (same FatFs f_sync path as wav_part.c
// on ESP-IDF). Sync failure is fail-loud (ERROR) with nothing deleted. Odd-tail truncation is logical first (header declares the
// even size, which is what decoders honor); a best-effort filesystem
// truncate() then shrinks the file when the platform supports it.
// Truncate failure never fails a recovery whose header + rename succeed.
//
// Never deletes: only rename() moves files (part -> wav, part ->
// quarantine). No remove()/unlink() path exists in this module.

#include "wav_recovery.h"

#include <dirent.h>
#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#include "wav_part.h"
#include "wav_rotation.h"

static bool has_part_suffix_local(const char *path) {
    size_t len;
    size_t suffix;
    if (path == NULL) {
        return false;
    }
    len = strlen(path);
    suffix = strlen(RECORDER_PART_SUFFIX);
    if (len < suffix) {
        return false;
    }
    return strcmp(path + len - suffix, RECORDER_PART_SUFFIX) == 0;
}

static bool path_exists_local(const char *path) {
    struct stat st;
    if (path == NULL) {
        return false;
    }
    return stat(path, &st) == 0;
}

static bool path_is_dir_local(const char *path) {
    struct stat st;
    if (path == NULL || stat(path, &st) != 0) {
        return false;
    }
    return S_ISDIR(st.st_mode);
}

static long file_size_local(const char *path, bool *ok) {
    struct stat st;
    if (ok != NULL) {
        *ok = false;
    }
    if (path == NULL || stat(path, &st) != 0) {
        return -1;
    }
    if (st.st_size < 0) {
        return -1;
    }
    if (ok != NULL) {
        *ok = true;
    }
    return (long)st.st_size;
}

static const char *basename_local(const char *path) {
    const char *slash;
    const char *back;
    const char *base;
    if (path == NULL) {
        return "";
    }
    slash = strrchr(path, '/');
    back = strrchr(path, '\\');
    base = path;
    if (slash != NULL && slash + 1 > base) {
        base = slash + 1;
    }
    if (back != NULL && back + 1 > base) {
        base = back + 1;
    }
    return base;
}

static uint16_t get_u16le(const uint8_t *p) {
    return (uint16_t)((uint16_t)p[0] | ((uint16_t)p[1] << 8));
}

static uint32_t get_u32le(const uint8_t *p) {
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
           ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

bool wav_recovery_is_part_path(const char *path) {
    return has_part_suffix_local(path);
}

bool wav_recovery_validate_header(const uint8_t header[RECORDER_WAV_HEADER_SIZE]) {
    if (header == NULL) {
        return false;
    }
    if (memcmp(header + 0, "RIFF", 4) != 0) {
        return false;
    }
    if (memcmp(header + 8, "WAVE", 4) != 0) {
        return false;
    }
    if (memcmp(header + 12, "fmt ", 4) != 0) {
        return false;
    }
    if (get_u32le(header + 16) != 16u) {
        return false;
    }
    if (get_u16le(header + 20) != 1u) {
        return false;
    }
    if (get_u16le(header + 22) != RECORDER_CHANNELS) {
        return false;
    }
    if (get_u32le(header + 24) != RECORDER_SAMPLE_RATE_HZ) {
        return false;
    }
    if (get_u32le(header + 28) != RECORDER_BYTE_RATE) {
        return false;
    }
    if (get_u16le(header + 32) != RECORDER_BLOCK_ALIGN) {
        return false;
    }
    if (get_u16le(header + 34) != RECORDER_BITS_PER_SAMPLE) {
        return false;
    }
    if (memcmp(header + 36, "data", 4) != 0) {
        return false;
    }
    // Bytes 4..7 (ChunkSize) and 40..43 (Subchunk2Size) are intentionally
    // unchecked: they are stale after a power loss and are recomputed
    // from the file size during recovery.
    return true;
}

bool wav_recovery_build_quarantine_path(const char *part_path,
                                        const char *quarantine_dir, char *out,
                                        size_t out_size) {
    const char *base;
    int needed;
    if (out == NULL || out_size == 0u) {
        return false;
    }
    out[0] = '\0';
    if (!has_part_suffix_local(part_path) || quarantine_dir == NULL ||
        quarantine_dir[0] == '\0') {
        return false;
    }
    base = basename_local(part_path);
    if (base[0] == '\0' || strlen(base) > 128u) {
        return false;
    }
    if (strchr(base, '/') != NULL || strchr(base, '\\') != NULL) {
        return false;
    }
    needed = snprintf(out, out_size, "%s/%s", quarantine_dir, base);
    if (needed < 0 || (size_t)needed >= out_size) {
        out[0] = '\0';
        return false;
    }
    return true;
}

bool wav_recovery_ensure_dir(const char *dir) {
    if (dir == NULL || dir[0] == '\0') {
        return false;
    }
    if (mkdir(dir, 0755) != 0) {
        if (errno != EEXIST) {
            return false;
        }
        // EEXIST may name a regular file; require a directory.
        if (!path_is_dir_local(dir)) {
            return false;
        }
    }
    return true;
}

// Move part_path into quarantine_dir (basename join with numeric
// uniqueness). On success writes the final destination to out_dest when
// provided and returns true. Never deletes the source on failure.
static bool quarantine_move(const char *part_path, const char *quarantine_dir,
                            char *out_dest, size_t out_size) {
    char candidate[RECORDER_MAX_PATH_LEN];
    size_t i;
    if (!has_part_suffix_local(part_path) || quarantine_dir == NULL ||
        quarantine_dir[0] == '\0') {
        return false;
    }
    if (!wav_recovery_ensure_dir(quarantine_dir)) {
        return false;
    }
    if (!wav_recovery_build_quarantine_path(part_path, quarantine_dir,
                                            candidate, sizeof(candidate))) {
        return false;
    }
    if (!path_exists_local(candidate)) {
        if (rename(part_path, candidate) == 0) {
            if (out_dest != NULL && out_size > 0u) {
                size_t n = strlen(candidate);
                if (n + 1u <= out_size) {
                    memcpy(out_dest, candidate, n + 1u);
                } else if (out_size > 0u) {
                    out_dest[0] = '\0';
                }
            }
            return true;
        }
        return false;
    }
    // Destination collision: append _1 .. _99 before the suffix.
    for (i = 1u; i < 100u; i++) {
        const char *base = basename_local(part_path);
        int needed;
        // Strip ".wav.part" (9 chars) then re-add with numeric infix.
        size_t base_len = strlen(base);
        char stem[160];
        size_t stem_len;
        if (base_len < strlen(RECORDER_PART_SUFFIX) + 1u) {
            return false;
        }
        stem_len = base_len - strlen(RECORDER_PART_SUFFIX);
        if (stem_len >= sizeof(stem)) {
            return false;
        }
        memcpy(stem, base, stem_len);
        stem[stem_len] = '\0';
        needed = snprintf(candidate, sizeof(candidate), "%s/%s_%u%s",
                          quarantine_dir, stem, (unsigned)i,
                          RECORDER_PART_SUFFIX);
        if (needed < 0 || (size_t)needed >= sizeof(candidate)) {
            return false;
        }
        if (!path_exists_local(candidate)) {
            if (rename(part_path, candidate) == 0) {
                if (out_dest != NULL && out_size > 0u) {
                    size_t n = strlen(candidate);
                    if (n + 1u <= out_size) {
                        memcpy(out_dest, candidate, n + 1u);
                    } else {
                        out_dest[0] = '\0';
                    }
                }
                return true;
            }
            return false;
        }
    }
    return false;
}

static bool rewrite_header_with_sync(const char *part_path,
                                     uint32_t recovered_pcm) {
    uint8_t header[RECORDER_WAV_HEADER_SIZE];
    FILE *fp = NULL;
    size_t wrote;
    wav_part_build_header(header, recovered_pcm, RECORDER_SAMPLE_RATE_HZ,
                          RECORDER_CHANNELS, RECORDER_BITS_PER_SAMPLE);
    fp = fopen(part_path, "r+b");
    if (fp == NULL) {
        return false;
    }
    if (fseek(fp, 0L, SEEK_SET) != 0) {
        fclose(fp);
        return false;
    }
    wrote = fwrite(header, 1, sizeof(header), fp);
    if (wrote != sizeof(header)) {
        fclose(fp);
        return false;
    }
    if (fflush(fp) != 0) {
        fclose(fp);
        return false;
    }
    {
        int fd = fileno(fp);
        if (fd < 0) {
            fclose(fp);
            return false;
        }
        if (fsync(fd) != 0) {
            fclose(fp);
            return false;
        }
    }
    if (fclose(fp) != 0) {
        return false;
    }
    // Best-effort filesystem shrink for an odd tail: the header already
    // declares the even size (what decoders honor), so a truncate failure
    // never fails the recovery itself.
    {
        bool size_ok = false;
        long size = file_size_local(part_path, &size_ok);
        long want = (long)(RECORDER_WAV_HEADER_SIZE + recovered_pcm);
        if (size_ok && size > want) {
            (void)truncate(part_path, (off_t)want);
        }
    }
    return true;
}

wav_recovery_outcome_t wav_recovery_recover_file(const char *part_path,
                                                 const char *wav_path,
                                                 const char *quarantine_dir,
                                                 uint32_t *out_pcm_bytes) {
    bool size_ok = false;
    long size = -1;
    uint32_t payload = 0;
    uint32_t recovered = 0;
    uint8_t header[RECORDER_WAV_HEADER_SIZE];
    FILE *probe = NULL;
    size_t got;
    char expect_wav[RECORDER_MAX_PATH_LEN];
    if (out_pcm_bytes != NULL) {
        *out_pcm_bytes = 0;
    }
    if (!has_part_suffix_local(part_path) || wav_path == NULL ||
        wav_path[0] == '\0' || quarantine_dir == NULL ||
        quarantine_dir[0] == '\0') {
        return WAV_RECOVERY_ERROR;
    }
    // Suffix correspondence: wav must be part minus ".part".
    if (!wav_rotation_part_to_wav(part_path, expect_wav, sizeof(expect_wav))) {
        return WAV_RECOVERY_ERROR;
    }
    if (strcmp(expect_wav, wav_path) != 0) {
        return WAV_RECOVERY_ERROR;
    }
    if (!path_exists_local(part_path)) {
        // Already finalized by an earlier boot: no second rename.
        if (path_exists_local(wav_path)) {
            return WAV_RECOVERY_RECOVERED;
        }
        return WAV_RECOVERY_ERROR;
    }
    // Collision guard: never overwrite an existing finalized file.
    if (path_exists_local(wav_path)) {
        if (quarantine_move(part_path, quarantine_dir, NULL, 0)) {
            return WAV_RECOVERY_QUARANTINED;
        }
        return WAV_RECOVERY_ERROR;
    }
    size = file_size_local(part_path, &size_ok);
    if (!size_ok || size < (long)RECORDER_WAV_HEADER_SIZE) {
        if (quarantine_move(part_path, quarantine_dir, NULL, 0)) {
            return WAV_RECOVERY_QUARANTINED;
        }
        return WAV_RECOVERY_ERROR;
    }
    probe = fopen(part_path, "rb");
    if (probe == NULL) {
        return WAV_RECOVERY_ERROR;
    }
    got = fread(header, 1, sizeof(header), probe);
    fclose(probe);
    probe = NULL;
    if (got != sizeof(header) ||
        !wav_recovery_validate_header(header)) {
        if (quarantine_move(part_path, quarantine_dir, NULL, 0)) {
            return WAV_RECOVERY_QUARANTINED;
        }
        return WAV_RECOVERY_ERROR;
    }
    payload = (uint32_t)((long)size - (long)RECORDER_WAV_HEADER_SIZE);
    // Sample-boundary rule (Spec #36): truncate only a misaligned tail.
    recovered = payload - (payload % RECORDER_BYTES_PER_SAMPLE);
    if (!rewrite_header_with_sync(part_path, recovered)) {
        return WAV_RECOVERY_ERROR;
    }
    if (rename(part_path, wav_path) != 0) {
        // Header is already durable on the `.part`; a rename failure
        // (e.g., transient FS error) leaves the file for retry.
        if (errno == EEXIST) {
            if (quarantine_move(part_path, quarantine_dir, NULL, 0)) {
                return WAV_RECOVERY_QUARANTINED;
            }
        }
        return WAV_RECOVERY_ERROR;
    }
    if (out_pcm_bytes != NULL) {
        *out_pcm_bytes = recovered;
    }
    return WAV_RECOVERY_RECOVERED;
}

bool wav_recovery_append_summary_event(const char *events_path,
                                       const wav_recovery_stats_t *stats) {
    FILE *fp = NULL;
    if (events_path == NULL || events_path[0] == '\0' || stats == NULL) {
        return false;
    }
    fp = fopen(events_path, "a");
    if (fp == NULL) {
        return false;
    }
    fprintf(fp,
            "{\"event\":\"recovery\",\"scanned\":%u,\"recovered\":%u,"
            "\"quarantined\":%u,\"errors\":%u,\"recovered_pcm_bytes\":%u}\n",
            (unsigned)stats->scanned, (unsigned)stats->recovered,
            (unsigned)stats->quarantined, (unsigned)stats->errors,
            (unsigned)stats->recovered_pcm_bytes);
    if (fflush(fp) != 0) {
        fclose(fp);
        return false;
    }
    {
        int fd = fileno(fp);
        if (fd >= 0) {
            (void)fsync(fd);
        }
    }
    if (fclose(fp) != 0) {
        return false;
    }
    return true;
}

static bool append_file_event(const char *events_path, const char *part_path,
                              const char *result, uint32_t pcm_bytes) {
    FILE *fp = NULL;
    const char *base;
    size_t i;
    if (events_path == NULL || events_path[0] == '\0' ||
        part_path == NULL || result == NULL) {
        return false;
    }
    fp = fopen(events_path, "a");
    if (fp == NULL) {
        return false;
    }
    // Basename only, sanitized to [A-Za-z0-9._-] so the JSON line cannot
    // break framing even with an unexpected on-card name. Full absolute
    // paths (which encode schedule metadata) are never written here.
    base = basename_local(part_path);
    fputs("{\"event\":\"recovery_file\",\"file\":\"", fp);
    for (i = 0; base[i] != '\0' && i < 128u; i++) {
        char c = base[i];
        bool ok = (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
                  (c >= '0' && c <= '9') || c == '.' || c == '_' ||
                  c == '-';
        fputc(ok ? c : '_', fp);
    }
    fprintf(fp, "\",\"result\":\"%s\",\"pcm_bytes\":%u}\n", result,
            (unsigned)pcm_bytes);
    if (fflush(fp) != 0) {
        fclose(fp);
        return false;
    }
    {
        int fd = fileno(fp);
        if (fd >= 0) {
            (void)fsync(fd);
        }
    }
    if (fclose(fp) != 0) {
        return false;
    }
    return true;
}

static void handle_one_part(const char *part_path, const char *quarantine_dir,
                            const char *events_path,
                            wav_recovery_stats_t *stats) {
    char wav_path[RECORDER_MAX_PATH_LEN];
    uint32_t pcm_bytes = 0;
    wav_recovery_outcome_t outcome;
    if (!wav_rotation_part_to_wav(part_path, wav_path, sizeof(wav_path))) {
        stats->scanned++;
        stats->errors++;
        return;
    }
    stats->scanned++;
    outcome = wav_recovery_recover_file(part_path, wav_path, quarantine_dir,
                                        &pcm_bytes);
    switch (outcome) {
        case WAV_RECOVERY_RECOVERED:
            stats->recovered++;
            stats->recovered_pcm_bytes += pcm_bytes;
            if (events_path != NULL) {
                (void)append_file_event(events_path, part_path, "recovered",
                                        pcm_bytes);
            }
            break;
        case WAV_RECOVERY_QUARANTINED:
            stats->quarantined++;
            stats->quarantined_files++;
            if (events_path != NULL) {
                (void)append_file_event(events_path, part_path,
                                        "quarantined", 0u);
            }
            break;
        case WAV_RECOVERY_ERROR:
        default:
            stats->errors++;
            if (events_path != NULL) {
                (void)append_file_event(events_path, part_path, "error", 0u);
            }
            break;
    }
}

bool wav_recovery_scan_recordings(const char *recordings_dir,
                                  const char *quarantine_dir,
                                  const char *events_path,
                                  wav_recovery_stats_t *out_stats) {
    DIR *dir = NULL;
    struct dirent *ent = NULL;
    wav_recovery_stats_t stats;
    if (out_stats == NULL) {
        return false;
    }
    memset(&stats, 0, sizeof(stats));
    *out_stats = stats;
    if (recordings_dir == NULL || recordings_dir[0] == '\0' ||
        quarantine_dir == NULL || quarantine_dir[0] == '\0') {
        return false;
    }
    if (!wav_recovery_ensure_dir(quarantine_dir)) {
        return false;
    }
    dir = opendir(recordings_dir);
    if (dir == NULL) {
        return false;
    }
    while ((ent = readdir(dir)) != NULL) {
        char child[RECORDER_MAX_PATH_LEN];
        int needed;
        if (strcmp(ent->d_name, ".") == 0 || strcmp(ent->d_name, "..") == 0) {
            continue;
        }
        needed = snprintf(child, sizeof(child), "%s/%s", recordings_dir,
                          ent->d_name);
        if (needed < 0 || (size_t)needed >= sizeof(child)) {
            stats.errors++;
            continue;
        }
        if (path_is_dir_local(child)) {
            DIR *sub = NULL;
            struct dirent *subent = NULL;
            sub = opendir(child);
            if (sub == NULL) {
                stats.errors++;
                continue;
            }
            while ((subent = readdir(sub)) != NULL) {
                char part[RECORDER_MAX_PATH_LEN];
                int n2;
                if (strcmp(subent->d_name, ".") == 0 ||
                    strcmp(subent->d_name, "..") == 0) {
                    continue;
                }
                n2 = snprintf(part, sizeof(part), "%s/%s", child,
                              subent->d_name);
                if (n2 < 0 || (size_t)n2 >= sizeof(part)) {
                    stats.errors++;
                    continue;
                }
                if (!has_part_suffix_local(part)) {
                    continue;
                }
                if (!path_is_dir_local(part)) {
                    handle_one_part(part, quarantine_dir, events_path,
                                    &stats);
                }
            }
            closedir(sub);
        } else {
            if (!has_part_suffix_local(child)) {
                continue;
            }
            handle_one_part(child, quarantine_dir, events_path, &stats);
        }
    }
    closedir(dir);
    *out_stats = stats;
    if (events_path != NULL) {
        (void)wav_recovery_append_summary_event(events_path, &stats);
    }
    return true;
}
