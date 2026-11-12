#include "qc_model.h"

#include <ctype.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#ifndef UA_ENABLE_METHODCALLS
#error "open62541 must be built with UA_ENABLE_METHODCALLS=ON"
#endif

#ifndef UA_ENABLE_SUBSCRIPTIONS_EVENTS
#error "open62541 must be built with UA_ENABLE_SUBSCRIPTIONS_EVENTS=ON"
#endif

#define QC_CHECK(expr) do {                         \
        UA_StatusCode qc_rc = (expr);               \
        if(qc_rc != UA_STATUSCODE_GOOD)             \
            return qc_rc;                           \
    } while(0)

static UA_NodeId
node_id(UA_UInt16 ns, const char *identifier) {
    return UA_NODEID_STRING(ns, (char*)(uintptr_t)identifier);
}

static UA_QualifiedName
qualified_name(UA_UInt16 ns, const char *name) {
    return UA_QUALIFIEDNAME(ns, (char*)(uintptr_t)name);
}

static UA_StatusCode
add_object(QCStationModel *model, UA_NodeId parent, const char *identifier,
           const char *browse_name, UA_NodeId *out) {
    UA_ObjectAttributes attr = UA_ObjectAttributes_default;
    attr.displayName = UA_LOCALIZEDTEXT("en-US", (char*)(uintptr_t)browse_name);
    attr.description = UA_LOCALIZEDTEXT("en-US", (char*)(uintptr_t)browse_name);
    return UA_Server_addObjectNode(model->server, node_id(model->ns, identifier),
                                   parent, UA_NODEID_NUMERIC(0, UA_NS0ID_HASCOMPONENT),
                                   qualified_name(model->ns, browse_name),
                                   UA_NODEID_NUMERIC(0, UA_NS0ID_BASEOBJECTTYPE),
                                   attr, NULL, out);
}

static UA_StatusCode
add_variable_full(QCStationModel *model, UA_NodeId parent, const char *identifier,
                  const char *browse_name, const void *value,
                  const UA_DataType *type, UA_Byte access,
                  UA_NodeId reference_type, UA_NodeId type_definition,
                  UA_NodeId *out) {
    UA_VariableAttributes attr = UA_VariableAttributes_default;
    attr.displayName = UA_LOCALIZEDTEXT("en-US", (char*)(uintptr_t)browse_name);
    attr.description = UA_LOCALIZEDTEXT("en-US", (char*)(uintptr_t)browse_name);
    attr.dataType = type->typeId;
    attr.valueRank = UA_VALUERANK_SCALAR;
    attr.accessLevel = access;
    attr.userAccessLevel = access;
    UA_Variant_setScalar(&attr.value, (void*)(uintptr_t)value, type);
    return UA_Server_addVariableNode(model->server, node_id(model->ns, identifier),
                                     parent, reference_type,
                                     qualified_name(model->ns, browse_name),
                                     type_definition, attr, NULL, out);
}

static UA_StatusCode
add_component_variable(QCStationModel *model, UA_NodeId parent,
                       const char *identifier, const char *browse_name,
                       const void *value, const UA_DataType *type,
                       UA_Byte access, UA_NodeId *out) {
    return add_variable_full(model, parent, identifier, browse_name, value, type,
                             access, UA_NODEID_NUMERIC(0, UA_NS0ID_HASCOMPONENT),
                             UA_NODEID_NUMERIC(0, UA_NS0ID_BASEDATAVARIABLETYPE), out);
}

static UA_StatusCode
add_event_property(QCStationModel *model, UA_NodeId event_type,
                   const char *browse_name, const void *value,
                   const UA_DataType *type) {
    char identifier[192];
    int written = snprintf(identifier, sizeof(identifier), "%s",
                           (const char*)(uintptr_t)browse_name);
    if(written <= 0 || (size_t)written >= sizeof(identifier))
        return UA_STATUSCODE_BADINTERNALERROR;

    /* Event properties are addressed by browse name. Give each one a stable,
     * readable NodeId below its event type instead of a random numeric id. */
    UA_NodeId event_type_node = event_type;
    UA_NodeId parent = event_type_node;
    const char *type_name = NULL;
    if(event_type_node.identifierType != UA_NODEIDTYPE_STRING ||
       !event_type_node.identifier.string.data)
        return UA_STATUSCODE_BADINTERNALERROR;
    type_name = (const char*)event_type_node.identifier.string.data;
    written = snprintf(identifier, sizeof(identifier), "%s.%s", type_name, browse_name);
    if(written <= 0 || (size_t)written >= sizeof(identifier))
        return UA_STATUSCODE_BADINTERNALERROR;

    char qualified[256];
    written = snprintf(qualified, sizeof(qualified), "%s", browse_name);
    if(written <= 0 || (size_t)written >= sizeof(qualified))
        return UA_STATUSCODE_BADINTERNALERROR;

    return add_variable_full(model, parent, identifier, qualified, value, type,
                             UA_ACCESSLEVELMASK_READ,
                             UA_NODEID_NUMERIC(0, UA_NS0ID_HASPROPERTY),
                             UA_NODEID_NUMERIC(0, UA_NS0ID_PROPERTYTYPE), NULL);
}

static UA_StatusCode
add_event_type(QCStationModel *model, const char *identifier,
               const char *browse_name, UA_NodeId *out) {
    UA_ObjectTypeAttributes attr = UA_ObjectTypeAttributes_default;
    attr.displayName = UA_LOCALIZEDTEXT("en-US", (char*)(uintptr_t)browse_name);
    attr.description = UA_LOCALIZEDTEXT("en-US", (char*)(uintptr_t)identifier);
    return UA_Server_addObjectTypeNode(model->server, node_id(model->ns, identifier),
                                       UA_NODEID_NUMERIC(0, UA_NS0ID_BASEEVENTTYPE),
                                       UA_NODEID_NUMERIC(0, UA_NS0ID_HASSUBTYPE),
                                       qualified_name(model->ns, browse_name),
                                       attr, NULL, out);
}

static UA_Argument
make_argument(const char *name, const UA_DataType *type) {
    UA_Argument arg;
    UA_Argument_init(&arg);
    arg.name = UA_STRING((char*)(uintptr_t)name);
    arg.description = UA_LOCALIZEDTEXT("en-US", (char*)(uintptr_t)name);
    arg.dataType = type->typeId;
    arg.valueRank = UA_VALUERANK_SCALAR;
    return arg;
}

static UA_StatusCode
write_int32(QCStationModel *model, UA_NodeId node, UA_Int32 value) {
    UA_Variant variant;
    UA_Variant_init(&variant);
    UA_Variant_setScalar(&variant, &value, &UA_TYPES[UA_TYPES_INT32]);
    return UA_Server_writeValue(model->server, node, variant);
}

static UA_StatusCode
write_uint32(QCStationModel *model, UA_NodeId node, UA_UInt32 value) {
    UA_Variant variant;
    UA_Variant_init(&variant);
    UA_Variant_setScalar(&variant, &value, &UA_TYPES[UA_TYPES_UINT32]);
    return UA_Server_writeValue(model->server, node, variant);
}

static UA_StatusCode
write_bool(QCStationModel *model, UA_NodeId node, UA_Boolean value) {
    UA_Variant variant;
    UA_Variant_init(&variant);
    UA_Variant_setScalar(&variant, &value, &UA_TYPES[UA_TYPES_BOOLEAN]);
    return UA_Server_writeValue(model->server, node, variant);
}

static UA_StatusCode
write_double(QCStationModel *model, UA_NodeId node, double value) {
    UA_Variant variant;
    UA_Variant_init(&variant);
    UA_Variant_setScalar(&variant, &value, &UA_TYPES[UA_TYPES_DOUBLE]);
    return UA_Server_writeValue(model->server, node, variant);
}

static UA_StatusCode
write_datetime_now(QCStationModel *model, UA_NodeId node) {
    UA_DateTime now = UA_DateTime_now();
    UA_Variant variant;
    UA_Variant_init(&variant);
    UA_Variant_setScalar(&variant, &now, &UA_TYPES[UA_TYPES_DATETIME]);
    return UA_Server_writeValue(model->server, node, variant);
}

static UA_StatusCode
write_string(QCStationModel *model, UA_NodeId node, const char *value) {
    UA_String text = UA_STRING((char*)(uintptr_t)(value ? value : ""));
    UA_Variant variant;
    UA_Variant_init(&variant);
    UA_Variant_setScalar(&variant, &text, &UA_TYPES[UA_TYPES_STRING]);
    return UA_Server_writeValue(model->server, node, variant);
}

static UA_StatusCode
read_int32(QCStationModel *model, UA_NodeId node, UA_Int32 *out) {
    UA_Variant variant;
    UA_Variant_init(&variant);
    UA_StatusCode rc = UA_Server_readValue(model->server, node, &variant);
    if(rc == UA_STATUSCODE_GOOD &&
       !UA_Variant_hasScalarType(&variant, &UA_TYPES[UA_TYPES_INT32]))
        rc = UA_STATUSCODE_BADTYPEMISMATCH;
    if(rc == UA_STATUSCODE_GOOD)
        *out = *(UA_Int32*)variant.data;
    UA_Variant_clear(&variant);
    return rc;
}

static UA_StatusCode
read_string(QCStationModel *model, UA_NodeId node, UA_String *out) {
    UA_Variant variant;
    UA_Variant_init(&variant);
    UA_String_init(out);
    UA_StatusCode rc = UA_Server_readValue(model->server, node, &variant);
    if(rc == UA_STATUSCODE_GOOD &&
       !UA_Variant_hasScalarType(&variant, &UA_TYPES[UA_TYPES_STRING]))
        rc = UA_STATUSCODE_BADTYPEMISMATCH;
    if(rc == UA_STATUSCODE_GOOD)
        rc = UA_String_copy((const UA_String*)variant.data, out);
    UA_Variant_clear(&variant);
    return rc;
}

static UA_Boolean
string_is_blank(const UA_String *value) {
    if(!value || value->length == 0)
        return UA_TRUE;
    for(size_t i = 0; i < value->length; i++) {
        if(!isspace((unsigned char)value->data[i]))
            return UA_FALSE;
    }
    return UA_TRUE;
}

static UA_Boolean
string_equals(const UA_String *left, const UA_String *right) {
    if(!left || !right || left->length != right->length)
        return UA_FALSE;
    if(left->length == 0)
        return UA_TRUE;
    return memcmp(left->data, right->data, left->length) == 0 ? UA_TRUE : UA_FALSE;
}

static void
string_to_cstr(const UA_String *value, char *buffer, size_t buffer_size) {
    if(!buffer || buffer_size == 0)
        return;
    if(!value || !value->data) {
        buffer[0] = '\0';
        return;
    }
    size_t copy_size = value->length;
    if(copy_size >= buffer_size)
        copy_size = buffer_size - 1;
    memcpy(buffer, value->data, copy_size);
    buffer[copy_size] = '\0';
}

static double
ng_rate(const QCStationModel *model) {
    if(model->ng_window_size == 0)
        return -1.0;
    size_t ng = 0;
    for(size_t i = 0; i < model->ng_window_size; i++) {
        if(!model->ng_window[i])
            ng++;
    }
    return (double)ng / (double)model->ng_window_size;
}

static UA_StatusCode
write_statistics(QCStationModel *model) {
    QC_CHECK(write_uint32(model, model->inspect_count_node, model->inspect_count));
    QC_CHECK(write_uint32(model, model->ok_count_node, model->ok_count));
    return write_uint32(model, model->ng_count_node, model->ng_count);
}

static UA_StatusCode
set_method_result(size_t output_size, UA_Variant *output, UA_Boolean accepted,
                  const char *message) {
    if(output_size < 2 || !output)
        return UA_STATUSCODE_BADINTERNALERROR;
    UA_StatusCode rc = UA_Variant_setScalarCopy(&output[0], &accepted,
                                                &UA_TYPES[UA_TYPES_BOOLEAN]);
    if(rc != UA_STATUSCODE_GOOD)
        return rc;
    UA_String text = UA_STRING((char*)(uintptr_t)(message ? message : ""));
    return UA_Variant_setScalarCopy(&output[1], &text, &UA_TYPES[UA_TYPES_STRING]);
}

static UA_StatusCode
event_write_string(QCStationModel *model, UA_NodeId event, const char *name,
                   const UA_String *value) {
    UA_String empty = UA_STRING("");
    const UA_String *actual = value ? value : &empty;
    return UA_Server_writeObjectProperty_scalar(model->server, event,
                                                qualified_name(model->ns, name),
                                                actual, &UA_TYPES[UA_TYPES_STRING]);
}

static UA_StatusCode
event_write_double(QCStationModel *model, UA_NodeId event, const char *name,
                   double value) {
    return UA_Server_writeObjectProperty_scalar(model->server, event,
                                                qualified_name(model->ns, name),
                                                &value, &UA_TYPES[UA_TYPES_DOUBLE]);
}

static UA_StatusCode
event_write_uint32(QCStationModel *model, UA_NodeId event, const char *name,
                   UA_UInt32 value) {
    return UA_Server_writeObjectProperty_scalar(model->server, event,
                                                qualified_name(model->ns, name),
                                                &value, &UA_TYPES[UA_TYPES_UINT32]);
}

static UA_StatusCode
prepare_event(QCStationModel *model, UA_NodeId event_type, UA_UInt16 severity,
              const char *message, UA_NodeId *out_event) {
    UA_StatusCode rc = UA_Server_createEvent(model->server, event_type, out_event);
    if(rc != UA_STATUSCODE_GOOD)
        return rc;

    UA_DateTime now = UA_DateTime_now();
    rc = UA_Server_writeObjectProperty_scalar(model->server, *out_event,
                                              UA_QUALIFIEDNAME(0, "Time"),
                                              &now, &UA_TYPES[UA_TYPES_DATETIME]);
    if(rc != UA_STATUSCODE_GOOD)
        return rc;

    rc = UA_Server_writeObjectProperty_scalar(model->server, *out_event,
                                              UA_QUALIFIEDNAME(0, "Severity"),
                                              &severity, &UA_TYPES[UA_TYPES_UINT16]);
    if(rc != UA_STATUSCODE_GOOD)
        return rc;

    UA_LocalizedText localized = UA_LOCALIZEDTEXT("en-US", (char*)(uintptr_t)message);
    rc = UA_Server_writeObjectProperty_scalar(model->server, *out_event,
                                              UA_QUALIFIEDNAME(0, "Message"),
                                              &localized, &UA_TYPES[UA_TYPES_LOCALIZEDTEXT]);
    if(rc != UA_STATUSCODE_GOOD)
        return rc;

    UA_String source_name = UA_STRING("QCStation.Events");
    rc = UA_Server_writeObjectProperty_scalar(model->server, *out_event,
                                              UA_QUALIFIEDNAME(0, "SourceName"),
                                              &source_name, &UA_TYPES[UA_TYPES_STRING]);
    if(rc != UA_STATUSCODE_GOOD)
        return rc;

    return UA_Server_writeObjectProperty_scalar(model->server, *out_event,
                                                UA_QUALIFIEDNAME(0, "SourceNode"),
                                                &model->events,
                                                &UA_TYPES[UA_TYPES_NODEID]);
}

static UA_StatusCode
trigger_event(QCStationModel *model, UA_NodeId event) {
    return UA_Server_triggerEvent(model->server, event, model->events,
                                  NULL, UA_TRUE);
}

static UA_StatusCode
emit_inspection_event(QCStationModel *model, UA_Boolean is_ok,
                      const UA_String *work_order, const UA_String *serial_number,
                      const char *defect_type, double confidence,
                      const char *image_id) {
    UA_NodeId event;
    char message[256];
    snprintf(message, sizeof(message), "Inspection %s: %s",
             image_id ? image_id : "", is_ok ? "OK" : "NG");

    QC_CHECK(prepare_event(model, model->inspection_event_type,
                           is_ok ? 1 : 3, message, &event));

    UA_String result = UA_STRING((char*)(uintptr_t)(is_ok ? "OK" : "NG"));
    UA_String defect = UA_STRING((char*)(uintptr_t)(defect_type ? defect_type : ""));
    UA_String image = UA_STRING((char*)(uintptr_t)(image_id ? image_id : ""));

    UA_StatusCode rc = event_write_string(model, event, "WorkOrderId", work_order);
    if(rc == UA_STATUSCODE_GOOD)
        rc = event_write_string(model, event, "SerialNumber", serial_number);
    if(rc == UA_STATUSCODE_GOOD)
        rc = event_write_string(model, event, "Result", &result);
    if(rc == UA_STATUSCODE_GOOD)
        rc = event_write_string(model, event, "DefectType", &defect);
    if(rc == UA_STATUSCODE_GOOD)
        rc = event_write_double(model, event, "Confidence", confidence);
    if(rc == UA_STATUSCODE_GOOD)
        rc = event_write_string(model, event, "ImageId", &image);
    if(rc == UA_STATUSCODE_GOOD)
        rc = trigger_event(model, event);
    UA_NodeId_clear(&event);
    return rc;
}

static UA_StatusCode
emit_ng_event(QCStationModel *model, const UA_String *work_order,
              const UA_String *serial_number, const char *defect_type,
              double confidence, const char *image_id) {
    UA_NodeId event;
    char message[256];
    snprintf(message, sizeof(message), "NG detected: %s", defect_type ? defect_type : "");

    QC_CHECK(prepare_event(model, model->ng_event_type, 3, message, &event));

    UA_String defect = UA_STRING((char*)(uintptr_t)(defect_type ? defect_type : ""));
    UA_String image = UA_STRING((char*)(uintptr_t)(image_id ? image_id : ""));

    UA_StatusCode rc = event_write_string(model, event, "WorkOrderId", work_order);
    if(rc == UA_STATUSCODE_GOOD)
        rc = event_write_string(model, event, "SerialNumber", serial_number);
    if(rc == UA_STATUSCODE_GOOD)
        rc = event_write_string(model, event, "DefectType", &defect);
    if(rc == UA_STATUSCODE_GOOD)
        rc = event_write_double(model, event, "Confidence", confidence);
    if(rc == UA_STATUSCODE_GOOD)
        rc = event_write_string(model, event, "ImageId", &image);
    if(rc == UA_STATUSCODE_GOOD)
        rc = trigger_event(model, event);
    UA_NodeId_clear(&event);
    return rc;
}

static UA_StatusCode
emit_completion_event(QCStationModel *model, const UA_String *work_order,
                      double rate) {
    UA_NodeId event;
    char message[256];
    snprintf(message, sizeof(message), "WorkOrder completed");

    QC_CHECK(prepare_event(model, model->completion_event_type, 2, message, &event));

    UA_StatusCode rc = event_write_string(model, event, "WorkOrderId", work_order);
    if(rc == UA_STATUSCODE_GOOD)
        rc = event_write_uint32(model, event, "InspectCount", model->inspect_count);
    if(rc == UA_STATUSCODE_GOOD)
        rc = event_write_uint32(model, event, "OkCount", model->ok_count);
    if(rc == UA_STATUSCODE_GOOD)
        rc = event_write_uint32(model, event, "NgCount", model->ng_count);
    if(rc == UA_STATUSCODE_GOOD)
        rc = event_write_double(model, event, "NgRate", rate);
    if(rc == UA_STATUSCODE_GOOD)
        rc = trigger_event(model, event);
    UA_NodeId_clear(&event);
    return rc;
}

static UA_StatusCode
start_inspection_callback(UA_Server *server, const UA_NodeId *session_id,
                          void *session_context, const UA_NodeId *method_id,
                          void *method_context, const UA_NodeId *object_id,
                          void *object_context, size_t input_size,
                          const UA_Variant *input, size_t output_size,
                          UA_Variant *output) {
    (void)server; (void)session_id; (void)method_id; (void)object_id; (void)object_context;
    QCStationModel *model = (QCStationModel*)method_context;
    if(!model || !model->is_admin || !model->is_admin(session_context))
        return UA_STATUSCODE_BADUSERACCESSDENIED;
    if(input_size != 2 || output_size != 2 ||
       !UA_Variant_hasScalarType(&input[0], &UA_TYPES[UA_TYPES_STRING]) ||
       !UA_Variant_hasScalarType(&input[1], &UA_TYPES[UA_TYPES_UINT32]))
        return UA_STATUSCODE_BADTYPEMISMATCH;

    const UA_String *work_order = (const UA_String*)input[0].data;
    UA_UInt32 target_count = *(const UA_UInt32*)input[1].data;
    if(string_is_blank(work_order))
        return set_method_result(output_size, output, UA_FALSE,
                                 "rejected: empty workOrderId");

    UA_Int32 status = 0;
    UA_StatusCode rc = read_int32(model, model->device_status, &status);
    if(rc != UA_STATUSCODE_GOOD)
        return rc;

    UA_String active;
    rc = read_string(model, model->current_work_order, &active);
    if(rc != UA_STATUSCODE_GOOD)
        return rc;

    char work_order_text[256];
    string_to_cstr(work_order, work_order_text, sizeof(work_order_text));

    if(status == QC_DEVICE_RUNNING) {
        if(string_equals(work_order, &active)) {
            UA_String_clear(&active);
            return set_method_result(output_size, output, UA_TRUE, "already running");
        }
        char message[384];
        char active_text[256];
        string_to_cstr(&active, active_text, sizeof(active_text));
        snprintf(message, sizeof(message),
                 "rejected: already running with different work order %s", active_text);
        UA_String_clear(&active);
        return set_method_result(output_size, output, UA_FALSE, message);
    }
    UA_String_clear(&active);

    if(status == QC_DEVICE_FAULT)
        return set_method_result(output_size, output, UA_FALSE,
                                 "rejected: DeviceStatus=Fault");
    if(target_count == 0)
        return set_method_result(output_size, output, UA_FALSE,
                                 "rejected: targetCount must be > 0");

    rc = write_int32(model, model->device_status, QC_DEVICE_RUNNING);
    if(rc != UA_STATUSCODE_GOOD)
        return rc;
    rc = write_string(model, model->current_work_order, work_order_text);
    if(rc != UA_STATUSCODE_GOOD)
        return rc;

    model->target_count = target_count;
    char message[384];
    snprintf(message, sizeof(message), "started %s, target=%u",
             work_order_text, (unsigned)target_count);
    return set_method_result(output_size, output, UA_TRUE, message);
}

static UA_StatusCode
complete_work_order_callback(UA_Server *server, const UA_NodeId *session_id,
                             void *session_context, const UA_NodeId *method_id,
                             void *method_context, const UA_NodeId *object_id,
                             void *object_context, size_t input_size,
                             const UA_Variant *input, size_t output_size,
                             UA_Variant *output) {
    (void)server; (void)session_id; (void)method_id; (void)object_id; (void)object_context;
    QCStationModel *model = (QCStationModel*)method_context;
    if(!model || !model->is_admin || !model->is_admin(session_context))
        return UA_STATUSCODE_BADUSERACCESSDENIED;
    if(input_size != 1 || output_size != 2 ||
       !UA_Variant_hasScalarType(&input[0], &UA_TYPES[UA_TYPES_STRING]))
        return UA_STATUSCODE_BADTYPEMISMATCH;

    const UA_String *requested = (const UA_String*)input[0].data;
    UA_String active;
    UA_StatusCode rc = read_string(model, model->current_work_order, &active);
    if(rc != UA_STATUSCODE_GOOD)
        return rc;

    if(active.length == 0) {
        UA_String_clear(&active);
        return set_method_result(output_size, output, UA_FALSE,
                                 "rejected: no active work order");
    }
    if(!string_equals(requested, &active)) {
        char message[384];
        char active_text[256];
        string_to_cstr(&active, active_text, sizeof(active_text));
        snprintf(message, sizeof(message),
                 "rejected: workOrderId mismatch, expected %s", active_text);
        UA_String_clear(&active);
        return set_method_result(output_size, output, UA_FALSE, message);
    }

    UA_Int32 status = 0;
    rc = read_int32(model, model->device_status, &status);
    if(rc != UA_STATUSCODE_GOOD) {
        UA_String_clear(&active);
        return rc;
    }
    if(status != QC_DEVICE_RUNNING) {
        char message[256];
        snprintf(message, sizeof(message),
                 "rejected: DeviceStatus=%d is not Running", (int)status);
        UA_String_clear(&active);
        return set_method_result(output_size, output, UA_FALSE, message);
    }

    double rate = ng_rate(model);
    rc = emit_completion_event(model, &active, rate);
    if(rc != UA_STATUSCODE_GOOD) {
        UA_String_clear(&active);
        return rc;
    }

    rc = write_int32(model, model->device_status, QC_DEVICE_IDLE);
    if(rc == UA_STATUSCODE_GOOD)
        rc = write_string(model, model->current_work_order, "");
    if(rc != UA_STATUSCODE_GOOD) {
        UA_String_clear(&active);
        return rc;
    }
    model->target_count = 0;

    char work_order_text[256];
    char message[512];
    string_to_cstr(&active, work_order_text, sizeof(work_order_text));
    snprintf(message, sizeof(message),
             "workOrder=%s inspect=%u ok=%u ng=%u ngRate=%.3f",
             work_order_text, (unsigned)model->inspect_count,
             (unsigned)model->ok_count, (unsigned)model->ng_count, rate);
    UA_String_clear(&active);
    return set_method_result(output_size, output, UA_TRUE, message);
}

static UA_StatusCode
reset_counters_callback(UA_Server *server, const UA_NodeId *session_id,
                        void *session_context, const UA_NodeId *method_id,
                        void *method_context, const UA_NodeId *object_id,
                        void *object_context, size_t input_size,
                        const UA_Variant *input, size_t output_size,
                        UA_Variant *output) {
    (void)server; (void)session_id; (void)method_id; (void)object_id; (void)object_context;
    (void)input_size; (void)input; (void)output_size; (void)output;
    QCStationModel *model = (QCStationModel*)method_context;
    if(!model || !model->is_admin || !model->is_admin(session_context))
        return UA_STATUSCODE_BADUSERACCESSDENIED;
    return qc_model_reset_counters(model);
}

UA_StatusCode
qc_model_init(QCStationModel *model, UA_Server *server,
              UA_UInt16 namespace_index, QCIsAdminFn is_admin) {
    if(!model || !server || namespace_index == 0 || !is_admin)
        return UA_STATUSCODE_BADINVALIDARGUMENT;
    if(model->initialized)
        return UA_STATUSCODE_BADINVALIDSTATE;

    model->server = server;
    model->ns = namespace_index;
    model->is_admin = is_admin;

    UA_ObjectAttributes root_attr = UA_ObjectAttributes_default;
    root_attr.displayName = UA_LOCALIZEDTEXT("en-US", "QCStation");
    root_attr.description = UA_LOCALIZEDTEXT("en-US", "QC station root object");
    QC_CHECK(UA_Server_addObjectNode(server, node_id(model->ns, "QCStation"),
                                     UA_NODEID_NUMERIC(0, UA_NS0ID_OBJECTSFOLDER),
                                     UA_NODEID_NUMERIC(0, UA_NS0ID_ORGANIZES),
                                     qualified_name(model->ns, "QCStation"),
                                     UA_NODEID_NUMERIC(0, UA_NS0ID_BASEOBJECTTYPE),
                                     root_attr, NULL, &model->station));

    QC_CHECK(add_object(model, model->station, "QCStation.Inspection", "Inspection",
                        &model->inspection));
    QC_CHECK(add_object(model, model->station, "QCStation.Statistics", "Statistics",
                        &model->statistics));
    QC_CHECK(add_object(model, model->station, "QCStation.Events", "Events",
                        &model->events));
    QC_CHECK(UA_Server_writeEventNotifier(server, model->events,
                                          UA_EVENTNOTIFIER_SUBSCRIBE_TO_EVENT));

    UA_Int32 idle = QC_DEVICE_IDLE;
    UA_String empty = UA_STRING("");
    UA_Boolean false_value = UA_FALSE;
    UA_DateTime epoch = UA_DateTime_fromUnixTime(0);
    double zero = 0.0;
    double no_rate = -1.0;
    UA_UInt32 zero_u32 = 0;

    const UA_Byte rw = UA_ACCESSLEVELMASK_READ | UA_ACCESSLEVELMASK_WRITE;
    const UA_Byte ro = UA_ACCESSLEVELMASK_READ;

    QC_CHECK(add_component_variable(model, model->station, "QCStation.DeviceStatus",
                                    "DeviceStatus", &idle,
                                    &UA_TYPES[UA_TYPES_INT32], rw, &model->device_status));
    QC_CHECK(add_component_variable(model, model->station, "QCStation.CurrentWorkOrderId",
                                    "CurrentWorkOrderId", &empty,
                                    &UA_TYPES[UA_TYPES_STRING], rw, &model->current_work_order));
    QC_CHECK(add_component_variable(model, model->station, "QCStation.CurrentPartSN",
                                    "CurrentPartSN", &empty,
                                    &UA_TYPES[UA_TYPES_STRING], rw, &model->current_part_sn));
    QC_CHECK(add_component_variable(model, model->station, "QCStation.LastMesOrderNo",
                                    "LastMesOrderNo", &empty,
                                    &UA_TYPES[UA_TYPES_STRING], rw, &model->last_mes_order_no));
    QC_CHECK(add_component_variable(model, model->station, "QCStation.LastNgMesOrderNo",
                                    "LastNgMesOrderNo", &empty,
                                    &UA_TYPES[UA_TYPES_STRING], rw, &model->last_ng_mes_order_no));
    QC_CHECK(add_component_variable(model, model->station, "QCStation.LastCompletionReceiptNo",
                                    "LastCompletionReceiptNo", &empty,
                                    &UA_TYPES[UA_TYPES_STRING], rw,
                                    &model->last_completion_receipt_no));

    QC_CHECK(add_component_variable(model, model->inspection,
                                    "QCStation.Inspection.LastResult", "LastResult",
                                    &false_value, &UA_TYPES[UA_TYPES_BOOLEAN], ro,
                                    &model->last_result));
    QC_CHECK(add_component_variable(model, model->inspection,
                                    "QCStation.Inspection.LastDefectType", "LastDefectType",
                                    &empty, &UA_TYPES[UA_TYPES_STRING], ro,
                                    &model->last_defect_type));
    QC_CHECK(add_component_variable(model, model->inspection,
                                    "QCStation.Inspection.LastConfidence", "LastConfidence",
                                    &zero, &UA_TYPES[UA_TYPES_DOUBLE], ro,
                                    &model->last_confidence));
    QC_CHECK(add_component_variable(model, model->inspection,
                                    "QCStation.Inspection.LastImageId", "LastImageId",
                                    &empty, &UA_TYPES[UA_TYPES_STRING], ro,
                                    &model->last_image_id));
    QC_CHECK(add_component_variable(model, model->inspection,
                                    "QCStation.Inspection.LastTimestamp", "LastTimestamp",
                                    &epoch, &UA_TYPES[UA_TYPES_DATETIME], ro,
                                    &model->last_timestamp));
    QC_CHECK(add_component_variable(model, model->inspection,
                                    "QCStation.Inspection.NgRate", "NgRate",
                                    &no_rate, &UA_TYPES[UA_TYPES_DOUBLE], ro,
                                    &model->ng_rate));

    QC_CHECK(add_component_variable(model, model->statistics,
                                    "QCStation.Statistics.InspectCount", "InspectCount",
                                    &zero_u32, &UA_TYPES[UA_TYPES_UINT32], ro,
                                    &model->inspect_count_node));
    QC_CHECK(add_component_variable(model, model->statistics,
                                    "QCStation.Statistics.OkCount", "OkCount",
                                    &zero_u32, &UA_TYPES[UA_TYPES_UINT32], ro,
                                    &model->ok_count_node));
    QC_CHECK(add_component_variable(model, model->statistics,
                                    "QCStation.Statistics.NgCount", "NgCount",
                                    &zero_u32, &UA_TYPES[UA_TYPES_UINT32], ro,
                                    &model->ng_count_node));

    QC_CHECK(add_event_type(model, "InspectionFinishedEventType",
                            "InspectionFinishedEventType", &model->inspection_event_type));
    QC_CHECK(add_event_property(model, model->inspection_event_type, "WorkOrderId",
                                &empty, &UA_TYPES[UA_TYPES_STRING]));
    QC_CHECK(add_event_property(model, model->inspection_event_type, "SerialNumber",
                                &empty, &UA_TYPES[UA_TYPES_STRING]));
    QC_CHECK(add_event_property(model, model->inspection_event_type, "Result",
                                &empty, &UA_TYPES[UA_TYPES_STRING]));
    QC_CHECK(add_event_property(model, model->inspection_event_type, "DefectType",
                                &empty, &UA_TYPES[UA_TYPES_STRING]));
    QC_CHECK(add_event_property(model, model->inspection_event_type, "Confidence",
                                &zero, &UA_TYPES[UA_TYPES_DOUBLE]));
    QC_CHECK(add_event_property(model, model->inspection_event_type, "ImageId",
                                &empty, &UA_TYPES[UA_TYPES_STRING]));

    QC_CHECK(add_event_type(model, "NgDetectedEventType", "NgDetectedEventType",
                            &model->ng_event_type));
    QC_CHECK(add_event_property(model, model->ng_event_type, "WorkOrderId",
                                &empty, &UA_TYPES[UA_TYPES_STRING]));
    QC_CHECK(add_event_property(model, model->ng_event_type, "SerialNumber",
                                &empty, &UA_TYPES[UA_TYPES_STRING]));
    QC_CHECK(add_event_property(model, model->ng_event_type, "DefectType",
                                &empty, &UA_TYPES[UA_TYPES_STRING]));
    QC_CHECK(add_event_property(model, model->ng_event_type, "Confidence",
                                &zero, &UA_TYPES[UA_TYPES_DOUBLE]));
    QC_CHECK(add_event_property(model, model->ng_event_type, "ImageId",
                                &empty, &UA_TYPES[UA_TYPES_STRING]));

    QC_CHECK(add_event_type(model, "WorkOrderCompletedEventType",
                            "WorkOrderCompletedEventType", &model->completion_event_type));
    QC_CHECK(add_event_property(model, model->completion_event_type, "WorkOrderId",
                                &empty, &UA_TYPES[UA_TYPES_STRING]));
    QC_CHECK(add_event_property(model, model->completion_event_type, "InspectCount",
                                &zero_u32, &UA_TYPES[UA_TYPES_UINT32]));
    QC_CHECK(add_event_property(model, model->completion_event_type, "OkCount",
                                &zero_u32, &UA_TYPES[UA_TYPES_UINT32]));
    QC_CHECK(add_event_property(model, model->completion_event_type, "NgCount",
                                &zero_u32, &UA_TYPES[UA_TYPES_UINT32]));
    QC_CHECK(add_event_property(model, model->completion_event_type, "NgRate",
                                &no_rate, &UA_TYPES[UA_TYPES_DOUBLE]));

    UA_MethodAttributes method_attr = UA_MethodAttributes_default;
    method_attr.executable = UA_TRUE;
    method_attr.userExecutable = UA_TRUE;
    method_attr.displayName = UA_LOCALIZEDTEXT("en-US", "StartInspection");
    method_attr.description = UA_LOCALIZEDTEXT("en-US", "Start a QC work order");

    UA_Argument start_inputs[2] = {
        make_argument("WorkOrderId", &UA_TYPES[UA_TYPES_STRING]),
        make_argument("TargetCount", &UA_TYPES[UA_TYPES_UINT32])
    };
    UA_Argument start_outputs[2] = {
        make_argument("Accepted", &UA_TYPES[UA_TYPES_BOOLEAN]),
        make_argument("Message", &UA_TYPES[UA_TYPES_STRING])
    };
    QC_CHECK(UA_Server_addMethodNode(server, node_id(model->ns, "QCStation.StartInspection"),
                                     model->station,
                                     UA_NODEID_NUMERIC(0, UA_NS0ID_HASCOMPONENT),
                                     qualified_name(model->ns, "StartInspection"),
                                     method_attr, start_inspection_callback,
                                     2, start_inputs, 2, start_outputs,
                                     model, &model->start_method));

    method_attr.displayName = UA_LOCALIZEDTEXT("en-US", "CompleteWorkOrder");
    method_attr.description = UA_LOCALIZEDTEXT("en-US", "Complete the active QC work order");
    UA_Argument complete_inputs[1] = {
        make_argument("WorkOrderId", &UA_TYPES[UA_TYPES_STRING])
    };
    UA_Argument complete_outputs[2] = {
        make_argument("Accepted", &UA_TYPES[UA_TYPES_BOOLEAN]),
        make_argument("Message", &UA_TYPES[UA_TYPES_STRING])
    };
    QC_CHECK(UA_Server_addMethodNode(server, node_id(model->ns, "QCStation.CompleteWorkOrder"),
                                     model->station,
                                     UA_NODEID_NUMERIC(0, UA_NS0ID_HASCOMPONENT),
                                     qualified_name(model->ns, "CompleteWorkOrder"),
                                     method_attr, complete_work_order_callback,
                                     1, complete_inputs, 2, complete_outputs,
                                     model, &model->complete_method));

    method_attr.displayName = UA_LOCALIZEDTEXT("en-US", "ResetCounters");
    method_attr.description = UA_LOCALIZEDTEXT("en-US", "Reset counters and NG window");
    QC_CHECK(UA_Server_addMethodNode(server, node_id(model->ns, "QCStation.ResetCounters"),
                                     model->station,
                                     UA_NODEID_NUMERIC(0, UA_NS0ID_HASCOMPONENT),
                                     qualified_name(model->ns, "ResetCounters"),
                                     method_attr, reset_counters_callback,
                                     0, NULL, 0, NULL, model, &model->reset_method));

    model->initialized = UA_TRUE;
    return UA_STATUSCODE_GOOD;
}

void
qc_model_clear(QCStationModel *model) {
    if(!model)
        return;
    UA_NodeId_clear(&model->station);
    UA_NodeId_clear(&model->inspection);
    UA_NodeId_clear(&model->statistics);
    UA_NodeId_clear(&model->events);
    UA_NodeId_clear(&model->device_status);
    UA_NodeId_clear(&model->current_work_order);
    UA_NodeId_clear(&model->current_part_sn);
    UA_NodeId_clear(&model->last_mes_order_no);
    UA_NodeId_clear(&model->last_ng_mes_order_no);
    UA_NodeId_clear(&model->last_completion_receipt_no);
    UA_NodeId_clear(&model->last_result);
    UA_NodeId_clear(&model->last_defect_type);
    UA_NodeId_clear(&model->last_confidence);
    UA_NodeId_clear(&model->last_image_id);
    UA_NodeId_clear(&model->last_timestamp);
    UA_NodeId_clear(&model->ng_rate);
    UA_NodeId_clear(&model->inspect_count_node);
    UA_NodeId_clear(&model->ok_count_node);
    UA_NodeId_clear(&model->ng_count_node);
    UA_NodeId_clear(&model->start_method);
    UA_NodeId_clear(&model->complete_method);
    UA_NodeId_clear(&model->reset_method);
    UA_NodeId_clear(&model->inspection_event_type);
    UA_NodeId_clear(&model->ng_event_type);
    UA_NodeId_clear(&model->completion_event_type);
    memset(model, 0, sizeof(*model));
}

UA_StatusCode
qc_model_record_inspection(QCStationModel *model, UA_Boolean is_ok,
                           const char *defect_type, double confidence,
                           const char *image_id, const char *serial_number) {
    if(!model || !model->initialized)
        return UA_STATUSCODE_BADINVALIDSTATE;

    UA_Int32 status = 0;
    UA_StatusCode rc = read_int32(model, model->device_status, &status);
    if(rc != UA_STATUSCODE_GOOD)
        return rc;
    if(status != QC_DEVICE_RUNNING)
        return UA_STATUSCODE_BADINVALIDSTATE;

    model->inspect_count++;
    if(is_ok)
        model->ok_count++;
    else
        model->ng_count++;

    if(model->ng_window_size < QC_NG_WINDOW_SIZE)
        model->ng_window_size++;
    model->ng_window[model->ng_window_head] = is_ok;
    model->ng_window_head = (model->ng_window_head + 1) % QC_NG_WINDOW_SIZE;

    UA_String work_order;
    rc = read_string(model, model->current_work_order, &work_order);
    if(rc != UA_STATUSCODE_GOOD)
        return rc;
    UA_String serial = UA_STRING((char*)(uintptr_t)(serial_number ? serial_number : ""));

    rc = write_bool(model, model->last_result, is_ok);
    if(rc == UA_STATUSCODE_GOOD)
        rc = write_string(model, model->last_defect_type, defect_type ? defect_type : "");
    if(rc == UA_STATUSCODE_GOOD)
        rc = write_double(model, model->last_confidence, confidence);
    if(rc == UA_STATUSCODE_GOOD)
        rc = write_string(model, model->last_image_id, image_id ? image_id : "");
    if(rc == UA_STATUSCODE_GOOD)
        rc = write_datetime_now(model, model->last_timestamp);
    if(rc == UA_STATUSCODE_GOOD && serial_number && serial_number[0] != '\0')
        rc = write_string(model, model->current_part_sn, serial_number);
    if(rc == UA_STATUSCODE_GOOD)
        rc = write_statistics(model);
    if(rc == UA_STATUSCODE_GOOD)
        rc = write_double(model, model->ng_rate, ng_rate(model));

    if(rc == UA_STATUSCODE_GOOD)
        rc = emit_inspection_event(model, is_ok, &work_order, &serial,
                                   defect_type ? defect_type : "", confidence,
                                   image_id ? image_id : "");
    if(rc == UA_STATUSCODE_GOOD && !is_ok)
        rc = emit_ng_event(model, &work_order, &serial,
                           defect_type ? defect_type : "", confidence,
                           image_id ? image_id : "");

    UA_String_clear(&work_order);
    return rc;
}

UA_StatusCode
qc_model_reset_counters(QCStationModel *model) {
    if(!model || !model->initialized)
        return UA_STATUSCODE_BADINVALIDSTATE;

    model->inspect_count = 0;
    model->ok_count = 0;
    model->ng_count = 0;
    model->ng_window_size = 0;
    model->ng_window_head = 0;
    memset(model->ng_window, 0, sizeof(model->ng_window));

    UA_StatusCode rc = write_statistics(model);
    if(rc == UA_STATUSCODE_GOOD)
        rc = write_double(model, model->ng_rate, -1.0);
    return rc;
}
