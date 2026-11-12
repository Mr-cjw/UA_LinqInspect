#include "qc_model.h"

#include <open62541/plugin/accesscontrol.h>
#include <open62541/plugin/log_stdout.h>
#include <open62541/plugin/pki_default.h>
#include <open62541/server.h>
#include <open62541/server_config_default.h>

#include <dirent.h>
#include <errno.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <unistd.h>

#ifndef UA_ENABLE_ENCRYPTION
#error "open62541 must be built with UA_ENABLE_ENCRYPTION=MBEDTLS"
#endif

#define QC_SECURITY_POLICY_URI \
    "http://opcfoundation.org/UA/SecurityPolicy#Basic256Sha256"
#define QC_DEFAULT_APPLICATION_URI "urn:fde:linqinspect:edge:pi5"
#define QC_ANONYMOUS_POLICY_ID "open62541-anonymous-policy"
#define QC_USERNAME_POLICY_ID "open62541-username-policy"
#define QC_MAX_PATH 4096

static volatile UA_Boolean running = UA_TRUE;

typedef struct {
    UA_Boolean admin;
    char username[128];
} QCSessionContext;

typedef struct {
    UA_ByteString *trust;
    size_t trust_size;
    UA_ByteString *revocation;
    size_t revocation_size;
} QCTrustMaterial;

static void
stop_handler(int signal_number) {
    (void)signal_number;
    running = UA_FALSE;
}

static UA_Boolean
is_blank(const char *value) {
    if(!value)
        return UA_TRUE;
    while(*value) {
        if(*value != ' ' && *value != '\t' && *value != '\r' && *value != '\n')
            return UA_FALSE;
        value++;
    }
    return UA_TRUE;
}

static const char *
required_env(const char *name) {
    const char *value = getenv(name);
    if(is_blank(value)) {
        fprintf(stderr,
                "startup error: %s is required (fail-closed security configuration)\n",
                name);
        return NULL;
    }
    return value;
}

static UA_Boolean
ends_with_ci(const char *value, const char *suffix) {
    size_t value_len = strlen(value);
    size_t suffix_len = strlen(suffix);
    if(value_len < suffix_len)
        return UA_FALSE;
    return strcasecmp(value + value_len - suffix_len, suffix) == 0 ? UA_TRUE : UA_FALSE;
}

static UA_Boolean
contains_ci(const char *value, const char *needle) {
    size_t needle_len = strlen(needle);
    if(needle_len == 0)
        return UA_TRUE;
    for(const char *p = value; *p; p++) {
        if(strncasecmp(p, needle, needle_len) == 0)
            return UA_TRUE;
    }
    return UA_FALSE;
}

/* 0 = ignore, 1 = trust certificate, 2 = revocation list. */
static int
classify_trust_file(const char *name) {
    if(!ends_with_ci(name, ".der") && !ends_with_ci(name, ".pem"))
        return 0;
    if(contains_ci(name, "key"))
        return 0;
    if(contains_ci(name, "crl"))
        return 2;
    return 1;
}

static UA_StatusCode
load_file(const char *path, UA_ByteString *out, char *error, size_t error_size) {
    UA_ByteString_init(out);
    FILE *file = fopen(path, "rb");
    if(!file) {
        snprintf(error, error_size, "cannot open %s: %s", path, strerror(errno));
        return UA_STATUSCODE_BADINTERNALERROR;
    }
    if(fseek(file, 0, SEEK_END) != 0) {
        snprintf(error, error_size, "cannot seek %s", path);
        fclose(file);
        return UA_STATUSCODE_BADINTERNALERROR;
    }
    long size = ftell(file);
    if(size <= 0) {
        snprintf(error, error_size, "%s is empty or unreadable", path);
        fclose(file);
        return UA_STATUSCODE_BADINTERNALERROR;
    }
    if(fseek(file, 0, SEEK_SET) != 0) {
        snprintf(error, error_size, "cannot rewind %s", path);
        fclose(file);
        return UA_STATUSCODE_BADINTERNALERROR;
    }
    out->data = (UA_Byte*)UA_malloc((size_t)size);
    if(!out->data) {
        snprintf(error, error_size, "out of memory reading %s", path);
        fclose(file);
        return UA_STATUSCODE_BADOUTOFMEMORY;
    }
    out->length = (size_t)size;
    size_t read_size = fread(out->data, 1, out->length, file);
    fclose(file);
    if(read_size != out->length) {
        snprintf(error, error_size, "short read from %s", path);
        UA_ByteString_clear(out);
        return UA_STATUSCODE_BADINTERNALERROR;
    }
    return UA_STATUSCODE_GOOD;
}

static void
free_trust_material(QCTrustMaterial *material) {
    if(!material)
        return;
    if(material->trust) {
        for(size_t i = 0; i < material->trust_size; i++)
            UA_ByteString_clear(&material->trust[i]);
        UA_Array_delete(material->trust, material->trust_size,
                        &UA_TYPES[UA_TYPES_BYTESTRING]);
    }
    if(material->revocation) {
        for(size_t i = 0; i < material->revocation_size; i++)
            UA_ByteString_clear(&material->revocation[i]);
        UA_Array_delete(material->revocation, material->revocation_size,
                        &UA_TYPES[UA_TYPES_BYTESTRING]);
    }
    memset(material, 0, sizeof(*material));
}

static UA_StatusCode
load_trust_material(const char *directory, QCTrustMaterial *material,
                    char *error, size_t error_size) {
    memset(material, 0, sizeof(*material));
    DIR *dir = opendir(directory);
    if(!dir) {
        snprintf(error, error_size, "cannot open UA_TRUSTED_CLIENT_CERTS_DIR=%s: %s",
                 directory, strerror(errno));
        return UA_STATUSCODE_BADINTERNALERROR;
    }

    size_t trust_count = 0;
    size_t revocation_count = 0;
    struct dirent *entry = NULL;
    while((entry = readdir(dir)) != NULL) {
        int kind = classify_trust_file(entry->d_name);
        if(kind == 1)
            trust_count++;
        else if(kind == 2)
            revocation_count++;
    }
    rewinddir(dir);

    if(trust_count == 0) {
        closedir(dir);
        snprintf(error, error_size,
                 "UA_TRUSTED_CLIENT_CERTS_DIR=%s contains no trustable .der/.pem CA certificate",
                 directory);
        return UA_STATUSCODE_BADINTERNALERROR;
    }

    material->trust = (UA_ByteString*)UA_Array_new(trust_count,
                                                   &UA_TYPES[UA_TYPES_BYTESTRING]);
    if(!material->trust) {
        closedir(dir);
        snprintf(error, error_size, "out of memory allocating trust list");
        return UA_STATUSCODE_BADOUTOFMEMORY;
    }
    material->trust_size = trust_count;

    if(revocation_count > 0) {
        material->revocation = (UA_ByteString*)UA_Array_new(
            revocation_count, &UA_TYPES[UA_TYPES_BYTESTRING]);
        if(!material->revocation) {
            closedir(dir);
            snprintf(error, error_size, "out of memory allocating revocation list");
            free_trust_material(material);
            return UA_STATUSCODE_BADOUTOFMEMORY;
        }
        material->revocation_size = revocation_count;
    }

    size_t trust_index = 0;
    size_t revocation_index = 0;
    while((entry = readdir(dir)) != NULL) {
        int kind = classify_trust_file(entry->d_name);
        if(kind == 0)
            continue;
        char path[QC_MAX_PATH];
        int written = snprintf(path, sizeof(path), "%s/%s", directory, entry->d_name);
        if(written <= 0 || (size_t)written >= sizeof(path)) {
            snprintf(error, error_size, "trust file path too long: %s", entry->d_name);
            closedir(dir);
            free_trust_material(material);
            return UA_STATUSCODE_BADINTERNALERROR;
        }
        UA_ByteString *target = (kind == 1)
            ? &material->trust[trust_index++]
            : &material->revocation[revocation_index++];
        UA_StatusCode rc = load_file(path, target, error, error_size);
        if(rc != UA_STATUSCODE_GOOD) {
            closedir(dir);
            free_trust_material(material);
            return rc;
        }
    }
    closedir(dir);
    return UA_STATUSCODE_GOOD;
}

static UA_Boolean
qc_session_is_admin(void *session_context) {
    QCSessionContext *context = (QCSessionContext*)session_context;
    return context && context->admin ? UA_TRUE : UA_FALSE;
}

static UA_Boolean
constant_time_equal(const UA_String *value, const char *expected) {
    if(!expected)
        return UA_FALSE;
    size_t expected_len = strlen(expected);
    size_t max_len = value->length > expected_len ? value->length : expected_len;
    unsigned char difference = (value->length == expected_len) ? 0 : 1;
    for(size_t i = 0; i < max_len; i++) {
        unsigned char supplied = i < value->length ? value->data[i] : 0;
        unsigned char wanted = i < expected_len ? (unsigned char)expected[i] : 0;
        difference |= (unsigned char)(supplied ^ wanted);
    }
    return difference == 0 ? UA_TRUE : UA_FALSE;
}

static UA_Boolean
policy_id_matches(const UA_String *policy_id, const char *expected) {
    if(!policy_id || policy_id->length == 0)
        return UA_FALSE;
    size_t expected_len = strlen(expected);
    if(policy_id->length < expected_len)
        return UA_FALSE;
    return strncmp((const char*)policy_id->data, expected, expected_len) == 0
        ? UA_TRUE : UA_FALSE;
}

static void
access_control_clear(UA_AccessControl *access_control) {
    if(!access_control)
        return;
    UA_Array_delete(access_control->userTokenPolicies,
                    access_control->userTokenPoliciesSize,
                    &UA_TYPES[UA_TYPES_USERTOKENPOLICY]);
    access_control->userTokenPolicies = NULL;
    access_control->userTokenPoliciesSize = 0;
}

static UA_StatusCode
access_control_init_tokens(UA_AccessControl *access_control) {
    access_control->userTokenPolicies = (UA_UserTokenPolicy*)UA_Array_new(
        2, &UA_TYPES[UA_TYPES_USERTOKENPOLICY]);
    if(!access_control->userTokenPolicies)
        return UA_STATUSCODE_BADOUTOFMEMORY;
    access_control->userTokenPoliciesSize = 2;

    UA_String policy_uri = UA_STRING_STATIC(QC_SECURITY_POLICY_URI);
    access_control->userTokenPolicies[0].tokenType = UA_USERTOKENTYPE_ANONYMOUS;
    access_control->userTokenPolicies[0].policyId =
        UA_STRING_ALLOC(QC_ANONYMOUS_POLICY_ID);
    UA_StatusCode rc = UA_String_copy(
        &policy_uri, &access_control->userTokenPolicies[0].securityPolicyUri);
    if(rc != UA_STATUSCODE_GOOD)
        return rc;

    access_control->userTokenPolicies[1].tokenType = UA_USERTOKENTYPE_USERNAME;
    access_control->userTokenPolicies[1].policyId =
        UA_STRING_ALLOC(QC_USERNAME_POLICY_ID);
    rc = UA_String_copy(
        &policy_uri, &access_control->userTokenPolicies[1].securityPolicyUri);
    return rc;
}

static UA_StatusCode
access_control_activate_session(UA_Server *server, UA_AccessControl *access_control,
                                const UA_EndpointDescription *endpoint_description,
                                const UA_ByteString *remote_certificate,
                                const UA_NodeId *session_id,
                                const UA_ExtensionObject *identity_token,
                                void **session_context) {
    (void)server; (void)access_control; (void)endpoint_description;
    (void)remote_certificate; (void)session_id;
    if(!session_context)
        return UA_STATUSCODE_BADINTERNALERROR;
    *session_context = NULL;
    if(!identity_token)
        return UA_STATUSCODE_BADIDENTITYTOKENINVALID;

    UA_AnonymousIdentityToken anonymous;
    UA_ExtensionObject normalized;
    if(identity_token->encoding == UA_EXTENSIONOBJECT_ENCODED_NOBODY) {
        UA_AnonymousIdentityToken_init(&anonymous);
        UA_ExtensionObject_init(&normalized);
        UA_ExtensionObject_setValueNoDelete(
            &normalized, &anonymous, &UA_TYPES[UA_TYPES_ANONYMOUSIDENTITYTOKEN]);
        identity_token = &normalized;
    }
    if(identity_token->encoding < UA_EXTENSIONOBJECT_DECODED)
        return UA_STATUSCODE_BADIDENTITYTOKENINVALID;

    const UA_DataType *token_type = identity_token->content.decoded.type;
    if(token_type == &UA_TYPES[UA_TYPES_ANONYMOUSIDENTITYTOKEN])
        return UA_STATUSCODE_GOOD;
    if(token_type != &UA_TYPES[UA_TYPES_USERNAMEIDENTITYTOKEN])
        return UA_STATUSCODE_BADIDENTITYTOKENINVALID;

    const UA_UserNameIdentityToken *token =
        (const UA_UserNameIdentityToken*)identity_token->content.decoded.data;
    if(!policy_id_matches(&token->policyId, QC_USERNAME_POLICY_ID))
        return UA_STATUSCODE_BADIDENTITYTOKENINVALID;
    if(token->userName.length == 0 || token->password.length == 0)
        return UA_STATUSCODE_BADUSERACCESSDENIED;

    const char *admin_user = getenv("UA_ADMIN_USER");
    const char *admin_pass = getenv("UA_ADMIN_PASS");
    if(is_blank(admin_user) || is_blank(admin_pass))
        return UA_STATUSCODE_BADUSERACCESSDENIED;

    UA_Boolean user_ok = constant_time_equal(&token->userName, admin_user);
    UA_Boolean password_ok = constant_time_equal(&token->password, admin_pass);
    if(!user_ok || !password_ok)
        return UA_STATUSCODE_BADUSERACCESSDENIED;

    QCSessionContext *context = (QCSessionContext*)UA_malloc(sizeof(QCSessionContext));
    if(!context)
        return UA_STATUSCODE_BADOUTOFMEMORY;
    memset(context, 0, sizeof(*context));
    context->admin = UA_TRUE;
    size_t copy_len = token->userName.length;
    if(copy_len >= sizeof(context->username))
        copy_len = sizeof(context->username) - 1;
    memcpy(context->username, token->userName.data, copy_len);
    context->username[copy_len] = '\0';
    *session_context = context;
    return UA_STATUSCODE_GOOD;
}

static void
access_control_close_session(UA_Server *server, UA_AccessControl *access_control,
                             const UA_NodeId *session_id, void *session_context) {
    (void)server; (void)access_control; (void)session_id;
    UA_free(session_context);
}

static UA_UInt32
access_control_rights_mask(UA_Server *server, UA_AccessControl *access_control,
                           const UA_NodeId *session_id, void *session_context,
                           const UA_NodeId *node_id, void *node_context) {
    (void)server; (void)access_control; (void)session_id;
    (void)node_id; (void)node_context;
    return qc_session_is_admin(session_context) ? 0xFFFFFFFFu : 0u;
}

static UA_Byte
access_control_access_level(UA_Server *server, UA_AccessControl *access_control,
                            const UA_NodeId *session_id, void *session_context,
                            const UA_NodeId *node_id, void *node_context) {
    (void)server; (void)access_control; (void)session_id;
    (void)node_id; (void)node_context;
    return qc_session_is_admin(session_context)
        ? (UA_Byte)0xFF : (UA_Byte)UA_ACCESSLEVELMASK_READ;
}

static UA_Boolean
access_control_executable(UA_Server *server, UA_AccessControl *access_control,
                          const UA_NodeId *session_id, void *session_context,
                          const UA_NodeId *method_id, void *method_context) {
    (void)server; (void)access_control; (void)session_id;
    (void)method_id; (void)method_context;
    return qc_session_is_admin(session_context);
}

static UA_Boolean
access_control_executable_on_object(UA_Server *server, UA_AccessControl *access_control,
                                    const UA_NodeId *session_id, void *session_context,
                                    const UA_NodeId *method_id, void *method_context,
                                    const UA_NodeId *object_id, void *object_context) {
    (void)server; (void)access_control; (void)session_id;
    (void)method_id; (void)method_context; (void)object_id; (void)object_context;
    return qc_session_is_admin(session_context);
}

static UA_Boolean
access_control_deny_add_node(UA_Server *server, UA_AccessControl *access_control,
                             const UA_NodeId *session_id, void *session_context,
                             const UA_AddNodesItem *item) {
    (void)server; (void)access_control; (void)session_id;
    (void)session_context; (void)item;
    return UA_FALSE;
}

static UA_Boolean
access_control_deny_add_reference(UA_Server *server, UA_AccessControl *access_control,
                                  const UA_NodeId *session_id, void *session_context,
                                  const UA_AddReferencesItem *item) {
    (void)server; (void)access_control; (void)session_id;
    (void)session_context; (void)item;
    return UA_FALSE;
}

static UA_Boolean
access_control_deny_delete_node(UA_Server *server, UA_AccessControl *access_control,
                                const UA_NodeId *session_id, void *session_context,
                                const UA_DeleteNodesItem *item) {
    (void)server; (void)access_control; (void)session_id;
    (void)session_context; (void)item;
    return UA_FALSE;
}

static UA_Boolean
access_control_deny_delete_reference(UA_Server *server, UA_AccessControl *access_control,
                                     const UA_NodeId *session_id, void *session_context,
                                     const UA_DeleteReferencesItem *item) {
    (void)server; (void)access_control; (void)session_id;
    (void)session_context; (void)item;
    return UA_FALSE;
}

static UA_Boolean
access_control_allow_browse(UA_Server *server, UA_AccessControl *access_control,
                            const UA_NodeId *session_id, void *session_context,
                            const UA_NodeId *node_id, void *node_context) {
    (void)server; (void)access_control; (void)session_id;
    (void)session_context; (void)node_id; (void)node_context;
    return UA_TRUE;
}

#ifdef UA_ENABLE_SUBSCRIPTIONS
static UA_Boolean
access_control_transfer_subscription(UA_Server *server, UA_AccessControl *access_control,
                                     const UA_NodeId *old_session_id,
                                     void *old_session_context,
                                     const UA_NodeId *new_session_id,
                                     void *new_session_context) {
    (void)server; (void)access_control; (void)old_session_id; (void)new_session_id;
    return qc_session_is_admin(old_session_context) ==
        qc_session_is_admin(new_session_context) ? UA_TRUE : UA_FALSE;
}
#endif

static UA_StatusCode
access_control_init(UA_AccessControl *access_control) {
    memset(access_control, 0, sizeof(*access_control));
    access_control->clear = access_control_clear;
    access_control->activateSession = access_control_activate_session;
    access_control->closeSession = access_control_close_session;
    access_control->getUserRightsMask = access_control_rights_mask;
    access_control->getUserAccessLevel = access_control_access_level;
    access_control->getUserExecutable = access_control_executable;
    access_control->getUserExecutableOnObject = access_control_executable_on_object;
    access_control->allowAddNode = access_control_deny_add_node;
    access_control->allowAddReference = access_control_deny_add_reference;
    access_control->allowDeleteNode = access_control_deny_delete_node;
    access_control->allowDeleteReference = access_control_deny_delete_reference;
    access_control->allowBrowseNode = access_control_allow_browse;
#ifdef UA_ENABLE_SUBSCRIPTIONS
    access_control->allowTransferSubscription = access_control_transfer_subscription;
#endif
    return access_control_init_tokens(access_control);
}

static UA_StatusCode
set_server_url(UA_ServerConfig *config, const char *bind_address, UA_UInt16 port) {
    UA_Array_delete(config->serverUrls, config->serverUrlsSize,
                    &UA_TYPES[UA_TYPES_STRING]);
    config->serverUrls = (UA_String*)UA_Array_new(1, &UA_TYPES[UA_TYPES_STRING]);
    if(!config->serverUrls)
        return UA_STATUSCODE_BADOUTOFMEMORY;
    config->serverUrlsSize = 1;

    char url[512];
    if(strcmp(bind_address, "0.0.0.0") == 0 || strcmp(bind_address, "*") == 0)
        snprintf(url, sizeof(url), "opc.tcp://:%u", (unsigned)port);
    else
        snprintf(url, sizeof(url), "opc.tcp://%s:%u", bind_address, (unsigned)port);

    config->serverUrls[0].data = (UA_Byte*)UA_malloc(strlen(url) + 1);
    if(!config->serverUrls[0].data)
        return UA_STATUSCODE_BADOUTOFMEMORY;
    memcpy(config->serverUrls[0].data, url, strlen(url));
    config->serverUrls[0].data[strlen(url)] = 0;
    config->serverUrls[0].length = strlen(url);
    return UA_STATUSCODE_GOOD;
}

static void
warn_if_certificate_expires_soon(const UA_ServerConfig *config,
                                 const UA_ByteString *certificate) {
    if(!config->secureChannelPKI.getExpirationDate)
        return;
    UA_DateTime expiry = 0;
    UA_ByteString mutable_certificate = *certificate;
    UA_StatusCode rc = config->secureChannelPKI.getExpirationDate(
        &expiry, &mutable_certificate);
    if(rc != UA_STATUSCODE_GOOD) {
        UA_LOG_WARNING(config->logging, UA_LOGCATEGORY_SERVER,
                       "Could not parse the server certificate expiry date (0x%08x)",
                       (unsigned)rc);
        return;
    }
    UA_DateTime now = UA_DateTime_now();
    double days = (double)(expiry - now) /
        ((double)UA_DATETIME_SEC * 24.0 * 60.0 * 60.0);
    if(days < 0.0) {
        UA_LOG_WARNING(config->logging, UA_LOGCATEGORY_SERVER,
                       "Server certificate is expired (%.1f days)", -days);
    } else if(days <= 30.0) {
        UA_LOG_WARNING(config->logging, UA_LOGCATEGORY_SERVER,
                       "Server certificate expires in %.1f days", days);
    } else {
        UA_LOG_INFO(config->logging, UA_LOGCATEGORY_SERVER,
                    "Server certificate expires in %.1f days", days);
    }
}

int
main(void) {
    signal(SIGINT, stop_handler);
    signal(SIGTERM, stop_handler);

    const char *cert_path = required_env("UA_SERVER_CERT");
    const char *key_path = required_env("UA_SERVER_PRIVATE_KEY");
    const char *trust_dir = required_env("UA_TRUSTED_CLIENT_CERTS_DIR");
    const char *admin_user = required_env("UA_ADMIN_USER");
    const char *admin_pass = required_env("UA_ADMIN_PASS");
    if(!cert_path || !key_path || !trust_dir || !admin_user || !admin_pass)
        return EXIT_FAILURE;

    const char *bind_address = getenv("UA_BIND_ADDRESS");
    if(is_blank(bind_address))
        bind_address = "127.0.0.1";
    const char *application_uri = getenv("UA_APPLICATION_URI");
    if(is_blank(application_uri))
        application_uri = QC_DEFAULT_APPLICATION_URI;

    unsigned long parsed_port = 4840;
    const char *port_text = getenv("UA_ENDPOINT_PORT");
    if(!is_blank(port_text)) {
        char *end = NULL;
        errno = 0;
        parsed_port = strtoul(port_text, &end, 10);
        if(errno != 0 || !end || *end != '\0' || parsed_port == 0 || parsed_port > 65535) {
            fprintf(stderr, "startup error: invalid UA_ENDPOINT_PORT=%s\n", port_text);
            return EXIT_FAILURE;
        }
    }
    UA_UInt16 port = (UA_UInt16)parsed_port;

    char error[512] = {0};
    UA_ByteString certificate = UA_BYTESTRING_NULL;
    UA_ByteString private_key = UA_BYTESTRING_NULL;
    if(load_file(cert_path, &certificate, error, sizeof(error)) != UA_STATUSCODE_GOOD ||
       load_file(key_path, &private_key, error, sizeof(error)) != UA_STATUSCODE_GOOD) {
        fprintf(stderr, "startup error: %s\n", error);
        UA_ByteString_clear(&certificate);
        UA_ByteString_clear(&private_key);
        return EXIT_FAILURE;
    }

    QCTrustMaterial trust_material;
    if(load_trust_material(trust_dir, &trust_material, error, sizeof(error)) !=
       UA_STATUSCODE_GOOD) {
        fprintf(stderr, "startup error: %s\n", error);
        UA_ByteString_clear(&certificate);
        UA_ByteString_clear(&private_key);
        return EXIT_FAILURE;
    }

    UA_ServerConfig config;
    memset(&config, 0, sizeof(config));
    UA_StatusCode rc = UA_ServerConfig_setBasics_withPort(&config, port);
    if(rc != UA_STATUSCODE_GOOD) {
        fprintf(stderr, "startup error: UA_ServerConfig_setBasics_withPort failed (0x%08x)\n",
                (unsigned)rc);
        goto cleanup_inputs;
    }

    if(config.sessionPKI.clear)
        config.sessionPKI.clear(&config.sessionPKI);
    if(config.secureChannelPKI.clear)
        config.secureChannelPKI.clear(&config.secureChannelPKI);

    rc = UA_CertificateVerification_Trustlist(
        &config.sessionPKI, trust_material.trust, trust_material.trust_size,
        NULL, 0, trust_material.revocation, trust_material.revocation_size);
    if(rc == UA_STATUSCODE_GOOD) {
        rc = UA_CertificateVerification_Trustlist(
            &config.secureChannelPKI, trust_material.trust, trust_material.trust_size,
            NULL, 0, trust_material.revocation, trust_material.revocation_size);
    }
    if(rc != UA_STATUSCODE_GOOD) {
        fprintf(stderr, "startup error: failed to load trusted client certificates (0x%08x)\n",
                (unsigned)rc);
        goto cleanup_config;
    }

    UA_String application_uri_string = UA_STRING((char*)(uintptr_t)application_uri);
    if(config.secureChannelPKI.verifyApplicationURI) {
        rc = config.secureChannelPKI.verifyApplicationURI(
            &config.secureChannelPKI, &certificate, &application_uri_string);
        if(rc != UA_STATUSCODE_GOOD) {
            fprintf(stderr,
                    "startup error: server certificate does not contain application URI %s (0x%08x)\n",
                    application_uri, (unsigned)rc);
            goto cleanup_config;
        }
    }

    rc = UA_ServerConfig_addSecurityPolicyBasic256Sha256(&config, &certificate, &private_key);
    if(rc != UA_STATUSCODE_GOOD) {
        fprintf(stderr,
                "startup error: cannot add Basic256Sha256 policy; check certificate/private-key match (0x%08x)\n",
                (unsigned)rc);
        goto cleanup_config;
    }

    UA_String security_policy = UA_STRING_STATIC(QC_SECURITY_POLICY_URI);
    rc = UA_ServerConfig_addEndpoint(&config, security_policy,
                                     UA_MESSAGESECURITYMODE_SIGNANDENCRYPT);
    if(rc != UA_STATUSCODE_GOOD) {
        fprintf(stderr, "startup error: cannot add Basic256Sha256 SignAndEncrypt endpoint (0x%08x)\n",
                (unsigned)rc);
        goto cleanup_config;
    }
    config.securityPolicyNoneDiscoveryOnly = true;
    config.allowNonePolicyPassword = false;

    rc = access_control_init(&config.accessControl);
    if(rc != UA_STATUSCODE_GOOD) {
        fprintf(stderr, "startup error: cannot initialize access control (0x%08x)\n",
                (unsigned)rc);
        goto cleanup_config;
    }

    rc = set_server_url(&config, bind_address, port);
    if(rc != UA_STATUSCODE_GOOD) {
        fprintf(stderr, "startup error: cannot set server listen URL (0x%08x)\n",
                (unsigned)rc);
        goto cleanup_config;
    }

    UA_String_clear(&config.applicationDescription.applicationUri);
    config.applicationDescription.applicationUri = UA_STRING_ALLOC(application_uri);
    config.applicationDescription.applicationType = UA_APPLICATIONTYPE_SERVER;
    if(!config.applicationDescription.applicationUri.data) {
        fprintf(stderr, "startup error: out of memory setting application URI\n");
        goto cleanup_config;
    }

    warn_if_certificate_expires_soon(&config, &certificate);

    UA_Server *server = UA_Server_newWithConfig(&config);
    if(!server) {
        fprintf(stderr, "startup error: open62541 server initialization failed\n");
        /* UA_Server_newWithConfig cleans the config on failure. */
        memset(&config, 0, sizeof(config));
        rc = UA_STATUSCODE_BADINTERNALERROR;
        goto cleanup_inputs;
    }

    UA_UInt16 namespace_index = UA_Server_addNamespace(server, QC_NS_URI);
    if(namespace_index == 0) {
        fprintf(stderr, "startup error: failed to register namespace %s\n", QC_NS_URI);
        rc = UA_STATUSCODE_BADINTERNALERROR;
        UA_Server_delete(server);
        goto cleanup_inputs;
    }

    QCStationModel model;
    memset(&model, 0, sizeof(model));
    rc = qc_model_init(&model, server, namespace_index, qc_session_is_admin);
    if(rc != UA_STATUSCODE_GOOD) {
        fprintf(stderr, "startup error: failed to build QCStation address space (0x%08x)\n",
                (unsigned)rc);
        qc_model_clear(&model);
        UA_Server_delete(server);
        goto cleanup_inputs;
    }

    rc = UA_Server_run_startup(server);
    if(rc != UA_STATUSCODE_GOOD) {
        fprintf(stderr, "startup error: UA_Server_run_startup failed (0x%08x)\n",
                (unsigned)rc);
        qc_model_clear(&model);
        UA_Server_delete(server);
        goto cleanup_inputs;
    }

    UA_LOG_INFO(UA_Log_Stdout, UA_LOGCATEGORY_SERVER,
                "QC OPC UA server listening on opc.tcp://%s:%u; policy=Basic256Sha256; mode=SignAndEncrypt; namespace=%u",
                bind_address, (unsigned)port, (unsigned)namespace_index);

    while(running) {
        /* run_iterate returns the next wait interval in milliseconds, not a
         * StatusCode. With waitInternal=true it blocks internally until the
         * next event or timeout. */
        (void)UA_Server_run_iterate(server, true);
    }

    UA_Server_run_shutdown(server);
    qc_model_clear(&model);
    UA_Server_delete(server);
    rc = UA_STATUSCODE_GOOD;

 cleanup_inputs:
    UA_ByteString_clear(&certificate);
    UA_ByteString_clear(&private_key);
    free_trust_material(&trust_material);
    return rc == UA_STATUSCODE_GOOD ? EXIT_SUCCESS : EXIT_FAILURE;

 cleanup_config:
    UA_ServerConfig_clean(&config);
    goto cleanup_inputs;
}
