#pragma once

#include <stdbool.h>
#include <stdint.h>

// #52 Device-owned ACK ingestion. The caller must have exclusive mounted SD
// ownership and must invoke this only in the accepted manual-WAKE recovery.
typedef struct {
    uint32_t applied;
    uint32_t already_applied;
    uint32_t unknown_ids;
    uint32_t invalid_payloads;
    uint32_t retention_candidates;
    uint32_t deleted_raw;  // PoC policy: always zero
} processed_ack_stats_t;

// Schema-invalid or foreign ACK is ignored with diagnostics, not promoted to
// a fatal recording failure. A genuine Device I/O/durable-state error fails
// closed (false) so recording cannot start under an uncertain apply state.
bool processed_ack_apply_manual_wake(const char *device_id,
                                     processed_ack_stats_t *out);
