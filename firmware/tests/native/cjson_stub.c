#include "cJSON.h"

#include <ctype.h>
#include <stdlib.h>
#include <string.h>

static cJSON *node_new(int type, const char *key, const char *value) {
    cJSON *node = (cJSON *)calloc(1u, sizeof(*node));
    if (node == NULL) {
        return NULL;
    }
    node->type = type;
    if (key != NULL) {
        node->string = strdup(key);
        if (node->string == NULL) {
            free(node);
            return NULL;
        }
    }
    if (value != NULL) {
        node->valuestring = strdup(value);
        if (node->valuestring == NULL) {
            free(node->string);
            free(node);
            return NULL;
        }
    }
    return node;
}

static void append_child(cJSON *parent, cJSON *child) {
    cJSON *tail;
    if (parent == NULL || child == NULL) {
        return;
    }
    if (parent->child == NULL) {
        parent->child = child;
        return;
    }
    tail = parent->child;
    while (tail->next != NULL) {
        tail = tail->next;
    }
    tail->next = child;
    child->prev = tail;
}

static const char *skip_ws(const char *p) {
    while (p != NULL && *p != '\0' && isspace((unsigned char)*p)) {
        ++p;
    }
    return p;
}

static char *extract_string_value(const char *json, const char *key) {
    char needle[64];
    const char *at;
    const char *p;
    const char *end;
    size_t len;
    char *out;

    if (snprintf(needle, sizeof(needle), "\"%s\"", key) <= 0) {
        return NULL;
    }
    at = strstr(json, needle);
    if (at == NULL) {
        return NULL;
    }
    p = skip_ws(at + strlen(needle));
    if (p == NULL || *p != ':') {
        return NULL;
    }
    p = skip_ws(p + 1);
    if (p == NULL || *p != '\"') {
        return NULL;
    }
    ++p;
    end = p;
    while (*end != '\0' && *end != '\"') {
        if (*end == '\\') {
            /* Harness vectors intentionally avoid escaped protocol fields. */
            return NULL;
        }
        ++end;
    }
    if (*end != '\"') {
        return NULL;
    }
    len = (size_t)(end - p);
    out = (char *)malloc(len + 1u);
    if (out == NULL) {
        return NULL;
    }
    memcpy(out, p, len);
    out[len] = '\0';
    return out;
}

static int member_type(const char *json, const char *key) {
    char needle[64];
    const char *at;
    const char *p;
    if (snprintf(needle, sizeof(needle), "\"%s\"", key) <= 0) {
        return 0;
    }
    at = strstr(json, needle);
    if (at == NULL) {
        return 0;
    }
    p = skip_ws(at + strlen(needle));
    if (p == NULL || *p != ':') {
        return 0;
    }
    p = skip_ws(p + 1);
    if (p == NULL) {
        return 0;
    }
    if (*p == '\"') {
        return CJSON_STUB_STRING;
    }
    if (*p == '{') {
        return CJSON_STUB_OBJECT;
    }
    if (*p == '[') {
        return CJSON_STUB_ARRAY;
    }
    return CJSON_STUB_NUMBER;
}

static char *extract_args_body(const char *json) {
    const char *at = strstr(json, "\"args\"");
    const char *p;
    const char *end;
    size_t len;
    char *out;
    if (at == NULL) {
        return NULL;
    }
    p = skip_ws(at + strlen("\"args\""));
    if (p == NULL || *p != ':') {
        return NULL;
    }
    p = skip_ws(p + 1);
    if (p == NULL || *p != '{') {
        return NULL;
    }
    ++p;
    end = strchr(p, '}');
    if (end == NULL) {
        return NULL;
    }
    len = (size_t)(end - p);
    out = (char *)malloc(len + 1u);
    if (out == NULL) {
        return NULL;
    }
    memcpy(out, p, len);
    out[len] = '\0';
    return out;
}

static cJSON *parse_object(const char *json) {
    cJSON *root = node_new(CJSON_STUB_OBJECT, NULL, NULL);
    char *value;
    char *args_body;
    int type;
    if (root == NULL) {
        return NULL;
    }

    type = member_type(json, "id");
    if (type != 0) {
        if (type == CJSON_STUB_STRING) {
            value = extract_string_value(json, "id");
            append_child(root, node_new(type, "id", value));
            free(value);
        } else {
            append_child(root, node_new(type, "id", NULL));
        }
    }

    type = member_type(json, "cmd");
    if (type != 0) {
        if (type == CJSON_STUB_STRING) {
            value = extract_string_value(json, "cmd");
            append_child(root, node_new(type, "cmd", value));
            free(value);
        } else {
            append_child(root, node_new(type, "cmd", NULL));
        }
    }

    type = member_type(json, "args");
    if (type != 0) {
        cJSON *args = node_new(type, "args", NULL);
        append_child(root, args);
        if (type == CJSON_STUB_OBJECT && args != NULL) {
            args_body = extract_args_body(json);
            if (args_body != NULL && *skip_ws(args_body) != '\0') {
                int time_type = member_type(args_body, "time");
                int release_type =
                    member_type(args_body, "releaseAttemptId");
                if (time_type != 0) {
                    if (time_type == CJSON_STUB_STRING) {
                        value = extract_string_value(args_body, "time");
                        append_child(args, node_new(time_type, "time", value));
                        free(value);
                    } else {
                        append_child(args, node_new(time_type, "time", NULL));
                    }
                }
                if (release_type != 0) {
                    if (release_type == CJSON_STUB_STRING) {
                        value = extract_string_value(
                            args_body, "releaseAttemptId");
                        append_child(
                            args,
                            node_new(release_type, "releaseAttemptId", value));
                        free(value);
                    } else {
                        append_child(
                            args,
                            node_new(release_type, "releaseAttemptId", NULL));
                    }
                }
                if (strstr(args_body, "\"extra\"") != NULL) {
                    append_child(args, node_new(CJSON_STUB_NUMBER, "extra", NULL));
                }
            }
            free(args_body);
        }
    }
    return root;
}

cJSON *cJSON_ParseWithOpts(const char *value, const char **return_parse_end,
                           int require_null_terminated) {
    const char *p;
    const char *end;
    (void)return_parse_end;
    (void)require_null_terminated;
    if (value == NULL) {
        return NULL;
    }
    p = skip_ws(value);
    if (p == NULL || *p == '\0') {
        return NULL;
    }
    end = value + strlen(value);
    while (end > p && isspace((unsigned char)end[-1])) {
        --end;
    }
    if (*p == '[' && end > p && end[-1] == ']') {
        return node_new(CJSON_STUB_ARRAY, NULL, NULL);
    }
    if (*p != '{' || end <= p || end[-1] != '}') {
        return NULL;
    }
    return parse_object(p);
}

void cJSON_Delete(cJSON *item) {
    while (item != NULL) {
        cJSON *next = item->next;
        if (item->child != NULL) {
            cJSON_Delete(item->child);
        }
        free(item->valuestring);
        free(item->string);
        free(item);
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
    if (object == NULL || string == NULL) {
        return NULL;
    }
    child = object->child;
    while (child != NULL) {
        if (child->string != NULL && strcmp(child->string, string) == 0) {
            return child;
        }
        child = child->next;
    }
    return NULL;
}

int cJSON_GetArraySize(const cJSON *array) {
    int count = 0;
    cJSON *child;
    if (array == NULL) {
        return 0;
    }
    child = array->child;
    while (child != NULL) {
        ++count;
        child = child->next;
    }
    return count;
}
