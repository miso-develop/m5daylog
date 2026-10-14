#pragma once

#include <stdbool.h>
#include <stdint.h>

// #52 / Decision #19: strictly older than seven full days, or usage >=80%.
// Never eligible without a durable processed marker. The implementation
// does not itself authorize deletion (PoC deletion switch remains OFF).
bool processed_ack_retention_eligible(bool processed, int64_t started_epoch,
                                      int64_t now_epoch, unsigned usage_percent);
