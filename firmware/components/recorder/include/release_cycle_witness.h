#pragma once
#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

// One immutable, current-process-boot release-cycle identity. The writer must
// call this only AFTER a nonzero-PCM finalized WAV hash is checked and its
// corresponding FINALIZED manifest upsert completes durably. No recovery,
// USB callback, host or RTC command is allowed to call this API.
bool release_cycle_witness_note_committed(const char *recording_id,
                                          uint32_t pcm_bytes);

// Read-only after the recorder writer's completed task barrier. Never reads
// SD, reads NVS, allocates, or changes the token. GET_STATUS must additionally
// require USB_SYNC/PC-owned and both boot/host lineage witnesses.
const char *release_cycle_witness_frozen_id(void);

#ifdef __cplusplus
}
#endif
