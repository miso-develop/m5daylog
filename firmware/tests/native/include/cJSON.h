#pragma once

#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct cJSON {
    struct cJSON *next;
    struct cJSON *prev;
    struct cJSON *child;
    int type;
    char *valuestring;
    int valueint;
    double valuedouble;
    char *string;
} cJSON;

#define CJSON_STUB_OBJECT 1
#define CJSON_STUB_ARRAY 2
#define CJSON_STUB_STRING 3
#define CJSON_STUB_NUMBER 4

cJSON *cJSON_ParseWithOpts(const char *value, const char **return_parse_end,
                           int require_null_terminated);
void cJSON_Delete(cJSON *item);
int cJSON_IsObject(const cJSON *item);
int cJSON_IsString(const cJSON *item);
cJSON *cJSON_GetObjectItemCaseSensitive(const cJSON *object,
                                        const char *string);
int cJSON_GetArraySize(const cJSON *array);

#ifdef __cplusplus
}
#endif
