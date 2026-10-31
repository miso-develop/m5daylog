#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include "release_cycle_button.h"

#define CHECK(c) do { if (!(c)) { \
  fprintf(stderr, "line=%d invariant=%s\\n",__LINE__,#c); exit(2); \
} } while (0)

int main(void) {
    release_cycle_button_detector_t s = {0};
    const uint64_t start = UINT64_C(100000000);
    CHECK(!release_cycle_button_detector_step(&s,true,true,start,0,1));
    CHECK(!release_cycle_button_detector_step(&s,true,true,start+9999999u,0,1));
    CHECK(release_cycle_button_detector_step(&s,true,true,start+10000000u,0,1));
    CHECK(!release_cycle_button_detector_step(&s,true,true,start+20000000u,0,1));

    s=(release_cycle_button_detector_t){0};
    CHECK(!release_cycle_button_detector_step(&s,true,true,start,0,1));
    // ISR catches release and re-press between two periodic task samples.
    CHECK(!release_cycle_button_detector_step(&s,true,true,start+9000000u,1,1));
    CHECK(!release_cycle_button_detector_step(&s,true,true,start+18000000u,1,1));
    CHECK(release_cycle_button_detector_step(&s,true,true,start+19000000u,1,1));

    s=(release_cycle_button_detector_t){0};
    CHECK(!release_cycle_button_detector_step(&s,true,true,start,0,1));
    CHECK(!release_cycle_button_detector_step(&s,true,false,start+9000000u,0,1));
    CHECK(!release_cycle_button_detector_step(&s,true,true,start+10000000u,0,1));
    CHECK(release_cycle_button_detector_step(&s,true,true,start+20000000u,0,1));

    s=(release_cycle_button_detector_t){0};
    CHECK(!release_cycle_button_detector_step(&s,true,true,start,0,1));
    CHECK(!release_cycle_button_detector_step(&s,true,true,start+9000000u,0,2));
    CHECK(!release_cycle_button_detector_step(&s,true,true,start+18000000u,0,2));
    CHECK(release_cycle_button_detector_step(&s,true,true,start+19000000u,0,2));

    s=(release_cycle_button_detector_t){0};
    CHECK(!release_cycle_button_detector_step(&s,true,true,start,0,1));
    CHECK(!release_cycle_button_detector_step(&s,false,true,start+9000000u,1,1));
    CHECK(!release_cycle_button_detector_step(&s,true,true,start+10000000u,1,1));
    CHECK(release_cycle_button_detector_step(&s,true,true,start+20000000u,1,1));

    s=(release_cycle_button_detector_t){0};
    CHECK(!release_cycle_button_detector_step(&s,true,true,start,0,1));
    CHECK(!release_cycle_button_detector_step(&s,true,true,start-1u,0,1));
    CHECK(!release_cycle_button_detector_step(&s,true,true,start+9999999u,0,1));
    CHECK(release_cycle_button_detector_step(&s,true,true,start+10000000u,0,1));

    puts("FU_BUTTON_DETECTOR=PASS");
    return 0;
}
