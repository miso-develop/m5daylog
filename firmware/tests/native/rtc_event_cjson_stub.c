#include "cJSON.h"

#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static const char *skip_ws(const char *p) {
    while (p != NULL && *p != '\0' && isspace((unsigned char)*p)) ++p;
    return p;
}

static cJSON *node_new(const char *key, const char *value) {
    cJSON *n = calloc(1u, sizeof(*n));
    if (n == NULL) return NULL;
    n->type = CJSON_STUB_STRING;
    if (key != NULL) n->string = strdup(key);
    if (value != NULL) n->valuestring = strdup(value);
    if ((key != NULL && n->string == NULL) ||
        (value != NULL && n->valuestring == NULL)) {
        free(n->string); free(n->valuestring); free(n); return NULL;
    }
    return n;
}

static char *extract(const char *json, const char *key) {
    char needle[64];
    const char *at;
    const char *p;
    const char *end;
    char *out;
    size_t len;

    if (snprintf(needle, sizeof(needle), "\"%s\"", key) <= 0) return NULL;
    at = strstr(json, needle);
    if (at == NULL) return NULL;
    p = skip_ws(at + strlen(needle));
    if (p == NULL || *p != ':') return NULL;
    p = skip_ws(p + 1);
    if (p == NULL || *p != '\"') return NULL;
    p++;
    end = p;
    while (*end != '\0' && *end != '\"') {
        if (*end == '\\') return NULL;
        end++;
    }
    if (*end != '\"') return NULL;
    len = (size_t)(end - p);
    out = malloc(len + 1u);
    if (out == NULL) return NULL;
    memcpy(out, p, len); out[len] = '\0';
    return out;
}

cJSON *cJSON_ParseWithOpts(const char *value, const char **return_parse_end,
                           int require_null_terminated) {
    static const char *keys[] = {
        "event", "correctionId", "before", "after", "source", "note"
    };
    const char *p;
    const char *end;
    cJSON *root;
    cJSON **tail;
    size_t i;
    (void)return_parse_end; (void)require_null_terminated;

    if (value == NULL) return NULL;
    p = skip_ws(value);
    end = value + strlen(value);
    while (end > p && isspace((unsigned char)end[-1])) end--;
    if (p == NULL || *p != '{' || end <= p || end[-1] != '}') return NULL;

    root = calloc(1u, sizeof(*root));
    if (root == NULL) return NULL;
    root->type = CJSON_STUB_OBJECT;
    tail = &root->child;
    for (i = 0; i < sizeof(keys)/sizeof(keys[0]); ++i) {
        char *v = extract(p, keys[i]);
        if (v != NULL) {
            cJSON *n = node_new(keys[i], v);
            free(v);
            if (n == NULL) { cJSON_Delete(root); return NULL; }
            *tail = n;
            tail = &n->next;
        }
    }
    return root;
}

void cJSON_Delete(cJSON *item) {
    while (item != NULL) {
        cJSON *next = item->next;
        if (item->child != NULL) cJSON_Delete(item->child);
        free(item->valuestring); free(item->string); free(item);
        item = next;
    }
}

int cJSON_IsObject(const cJSON *item) {
    return item != NULL && item->type == CJSON_STUB_OBJECT;
}
int cJSON_IsString(const cJSON *item) {
    return item != NULL && item->type == CJSON_STUB_STRING;
}
cJSON *cJSON_GetObjectItemCaseSensitive(const cJSON *object,
                                        const char *string) {
    cJSON *child;
    if (object == NULL || string == NULL) return NULL;
    for (child = object->child; child != NULL; child = child->next) {
        if (child->string != NULL && strcmp(child->string, string) == 0) {
            return child;
        }
    }
    return NULL;
}
int cJSON_GetArraySize(const cJSON *array) {
    int n = 0; const cJSON *child;
    for (child = array != NULL ? array->child : NULL;
         child != NULL; child = child->next) n++;
    return n;
}
