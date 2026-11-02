#pragma once
#include <stdbool.h>

// Source-only DEV installed-generation proof using read-only file access.
// Called after a selected-card CID/capacity check and before any recorder
// directory creation. Initial boot additionally requires pristine index.
// Never formats, repairs, creates files or authorizes a USB release.
bool dev_sdb11_media_readback_matches(const char *generation,
                                      const char *device_id,
                                      const char *binding,
                                      bool first_boot);
