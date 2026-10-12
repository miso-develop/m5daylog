// Bounded test-only JSON parser presenting the subset of cJSON used by #52.
// Production always links ESP-IDF's actual cJSON, never this implementation.
// No escapes are accepted in synthetic test vectors; malformed input fails.
#include "cJSON.h"

#include <ctype.h>
#include <errno.h>
#include <math.h>
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>

enum { TEST_OBJECT = 1, TEST_ARRAY = 2, TEST_STRING = 3,
       TEST_NUMBER = 4, TEST_OTHER = 5 };

typedef struct { const char *p; const char *end; unsigned depth; } parser_t;

static void ws(parser_t *s) {
    while (s->p < s->end && isspace((unsigned char)*s->p)) ++s->p;
}

static cJSON *node(int type) {
    cJSON *x = calloc(1u, sizeof(*x));
    if (x != NULL) x->type = type;
    return x;
}

void cJSON_Delete(cJSON *x) {
    while (x != NULL) {
        cJSON *next = x->next;
        cJSON_Delete(x->child);
        free(x->string);
        free(x->valuestring);
        free(x);
        x = next;
    }
}

static char *string(parser_t *s) {
    const char *begin;
    char *out;
    size_t n;
    if (s->p == s->end || *s->p++ != '"') return NULL;
    begin = s->p;
    while (s->p < s->end && *s->p != '"') {
        // Synthetic vectors avoid escape syntax; reject rather than
        // inaccurately interpreting user-supplied backslash sequences.
        if ((unsigned char)*s->p < 0x20u || *s->p == '\\') return NULL;
        ++s->p;
    }
    if (s->p == s->end) return NULL;
    n = (size_t)(s->p - begin);
    out = malloc(n+1u);
    if (out == NULL) return NULL;
    memcpy(out, begin, n);
    out[n] = '\0';
    ++s->p;
    return out;
}

static cJSON *value(parser_t *s);

static bool append(cJSON *parent, cJSON *child) {
    cJSON *tail;
    if (child == NULL) return false;
    if (parent->child == NULL) {
        parent->child = child;
        return true;
    }
    tail = parent->child;
    while (tail->next != NULL) tail = tail->next;
    tail->next = child;
    child->prev = tail;
    return true;
}

static cJSON *object(parser_t *s) {
    cJSON *o = node(TEST_OBJECT);
    if (o == NULL) return NULL;
    ++s->p;
    ws(s);
    if (s->p < s->end && *s->p == '}') {
        ++s->p;
        return o;
    }
    for (;;) {
        char *key = string(s);
        cJSON *child;
        if (key == NULL) break;
        ws(s);
        if (s->p == s->end || *s->p++ != ':') {
            free(key);
            break;
        }
        child = value(s);
        if (child == NULL) {
            free(key);
            break;
        }
        child->string = key;
        if (!append(o, child)) break;
        ws(s);
        if (s->p == s->end) break;
        if (*s->p == '}') {
            ++s->p;
            return o;
        }
        if (*s->p++ != ',') break;
        ws(s);
    }
    cJSON_Delete(o);
    return NULL;
}

static cJSON *array(parser_t *s) {
    cJSON *a = node(TEST_ARRAY);
    if (a == NULL) return NULL;
    ++s->p;
    ws(s);
    if (s->p < s->end && *s->p == ']') {
        ++s->p;
        return a;
    }
    for (;;) {
        cJSON *child = value(s);
        if (!append(a, child)) break;
        ws(s);
        if (s->p == s->end) break;
        if (*s->p == ']') {
            ++s->p;
            return a;
        }
        if (*s->p++ != ',') break;
        ws(s);
    }
    cJSON_Delete(a);
    return NULL;
}

static cJSON *number(parser_t *s) {
    char *endptr;
    cJSON *out;
    double d;
    errno = 0;
    d = strtod(s->p, &endptr);
    if (errno == ERANGE || endptr == s->p || endptr > s->end ||
        !isfinite(d)) return NULL;
    s->p = endptr;
    out = node(TEST_NUMBER);
    if (out != NULL) out->valuedouble = d;
    return out;
}

static cJSON *value(parser_t *s) {
    cJSON *out = NULL;
    ws(s);
    if (s->p == s->end || ++s->depth > 8u) {
        --s->depth;
        return NULL;
    }
    if (*s->p == '{') out = object(s);
    else if (*s->p == '[') out = array(s);
    else if (*s->p == '"') {
        char *v = string(s);
        if (v != NULL) {
            out = node(TEST_STRING);
            if (out != NULL) out->valuestring = v;
            else free(v);
        }
    } else if (*s->p == '-' || (*s->p >= '0' && *s->p <= '9')) {
        out = number(s);
    } else if (s->end - s->p >= 4 && !memcmp(s->p, "null", 4)) {
        out = node(TEST_OTHER);
        s->p += 4;
    } else if (s->end - s->p >= 4 && !memcmp(s->p, "true", 4)) {
        out = node(TEST_OTHER);
        s->p += 4;
    } else if (s->end - s->p >= 5 && !memcmp(s->p, "false", 5)) {
        out = node(TEST_OTHER);
        s->p += 5;
    }
    --s->depth;
    return out;
}

cJSON *cJSON_ParseWithLengthOpts(const char *src, size_t len,
                                const char **parse_end,
                                int require_null_terminated) {
    parser_t s;
    cJSON *root;
    if (src == NULL || len == 0u) return NULL;
    if (require_null_terminated &&
        (src[len-1u] != '\0' || memchr(src, '\0', len-1u) != NULL)) {
        return NULL;
    }
    s.p = src;
    s.end = src + (require_null_terminated ? len-1u : len);
    s.depth = 0u;
    root = value(&s);
    if (root == NULL) return NULL;
    ws(&s);
    if (parse_end != NULL) *parse_end = s.p;
    if (s.p != s.end) {
        cJSON_Delete(root);
        return NULL;
    }
    return root;
}

int cJSON_IsObject(const cJSON *x) { return x != NULL && x->type == TEST_OBJECT; }
int cJSON_IsArray(const cJSON *x) { return x != NULL && x->type == TEST_ARRAY; }
int cJSON_IsString(const cJSON *x) { return x != NULL && x->type == TEST_STRING; }
int cJSON_IsNumber(const cJSON *x) { return x != NULL && x->type == TEST_NUMBER; }
cJSON *cJSON_GetObjectItemCaseSensitive(const cJSON *x, const char *key) {
    cJSON *child;
    if (x == NULL || key == NULL) return NULL;
    for (child = x->child; child != NULL; child = child->next) {
        if (child->string != NULL && strcmp(child->string, key) == 0)
            return child;
    }
    return NULL;
}
cJSON *cJSON_GetArrayItem(const cJSON *x, int at) {
    cJSON *child;
    if (x == NULL || at < 0) return NULL;
    for (child = x->child; child != NULL && at > 0; child = child->next)
        --at;
    return child;
}
int cJSON_GetArraySize(const cJSON *x) {
    int n = 0;
    const cJSON *child;
    if (x == NULL) return 0;
    for (child = x->child; child != NULL; child = child->next) ++n;
    return n;
}
