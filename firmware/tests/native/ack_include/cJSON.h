#pragma once
// ABI-compatible public cJSON declarations for the native ACK harness.
// The implementation is the system's real libcjson shared library.
#include <stddef.h>
typedef struct cJSON {
    struct cJSON *next, *prev, *child;
    int type;
    char *valuestring;
    int valueint;
    double valuedouble;
    char *string;
} cJSON;
cJSON *cJSON_ParseWithLengthOpts(const char *, size_t, const char **, int);
void cJSON_Delete(cJSON *);
int cJSON_IsObject(const cJSON *);
int cJSON_IsArray(const cJSON *);
int cJSON_IsNumber(const cJSON *);
int cJSON_IsString(const cJSON *);
cJSON *cJSON_GetObjectItemCaseSensitive(const cJSON *, const char *);
cJSON *cJSON_GetArrayItem(const cJSON *, int);
int cJSON_GetArraySize(const cJSON *);
