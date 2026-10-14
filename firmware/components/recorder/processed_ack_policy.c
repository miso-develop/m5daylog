#include "processed_ack_policy.h"

bool processed_ack_retention_eligible(bool processed, int64_t started_epoch,
                                      int64_t now_epoch, unsigned usage_percent) {
    if (!processed || started_epoch < 0 || now_epoch < started_epoch ||
        usage_percent > 100u) {
        return false;
    }
    return usage_percent >= 80u ||
           (now_epoch - started_epoch) > (int64_t)(7u * 24u * 60u * 60u);
}
