#include "release_cycle_witness.h"

#include <stddef.h>
#include <string.h>

#define RELEASE_CYCLE_UUID_BYTES 37u

// A single writer task is the sole producer. CDC is not opened for GET_STATUS
// until the writer's finalization + APP->USB ownership barrier completes.
// No code ever resets this once-frozen value within the same boot.
static char s_frozen_uuid[RELEASE_CYCLE_UUID_BYTES];
static bool s_frozen;

static bool canonical_lower_uuid(const char *id) {
    size_t i;
    if (id == NULL || strlen(id) != RELEASE_CYCLE_UUID_BYTES - 1u) {
        return false;
    }
    for (i = 0u; i < RELEASE_CYCLE_UUID_BYTES - 1u; ++i) {
        const char c = id[i];
        if (i == 8u || i == 13u || i == 18u || i == 23u) {
            if (c != '-') return false;
        } else if (!((c >= '0' && c <= '9') ||
                     (c >= 'a' && c <= 'f'))) {
            return false;
        }
    }
    return true;
}

bool release_cycle_witness_note_committed(const char *recording_id,
                                          uint32_t pcm_bytes) {
    if (s_frozen || pcm_bytes == 0u || (pcm_bytes & 1u) != 0u ||
        !canonical_lower_uuid(recording_id)) {
        return false;
    }
    memcpy(s_frozen_uuid, recording_id, RELEASE_CYCLE_UUID_BYTES);
    s_frozen = true;
    return true;
}

const char *release_cycle_witness_frozen_id(void) {
    return s_frozen ? s_frozen_uuid : NULL;
}
