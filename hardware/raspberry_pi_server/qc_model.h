#ifndef QC_MODEL_H_
#define QC_MODEL_H_

#include <stddef.h>

#include <open62541/server.h>

#ifdef __cplusplus
extern "C" {
#endif

#define QC_NS_URI "urn:fde:linqinspect:qc"
#define QC_NG_WINDOW_SIZE 20

/* DeviceStatus values are part of the frozen M1 contract. */
typedef enum {
    QC_DEVICE_IDLE = 0,
    QC_DEVICE_RUNNING = 1,
    QC_DEVICE_FAULT = 2
} QCDeviceStatus;

/* The authorization function is supplied by main.c. It must return true only
 * for a successfully authenticated admin session. A trusted client certificate
 * or an anonymous session must never produce true. */
typedef UA_Boolean (*QCIsAdminFn)(void *sessionContext);

typedef struct QCStationModel {
    UA_Server *server;
    UA_UInt16 ns;
    QCIsAdminFn is_admin;
    UA_Boolean initialized;

    /* QCStation object tree. */
    UA_NodeId station;
    UA_NodeId inspection;
    UA_NodeId statistics;
    UA_NodeId events;

    /* QCStation variables. */
    UA_NodeId device_status;
    UA_NodeId current_work_order;
    UA_NodeId current_part_sn;
    UA_NodeId last_mes_order_no;
    UA_NodeId last_ng_mes_order_no;
    UA_NodeId last_completion_receipt_no;

    /* Inspection variables. */
    UA_NodeId last_result;
    UA_NodeId last_defect_type;
    UA_NodeId last_confidence;
    UA_NodeId last_image_id;
    UA_NodeId last_timestamp;
    UA_NodeId ng_rate;

    /* Statistics variables. */
    UA_NodeId inspect_count_node;
    UA_NodeId ok_count_node;
    UA_NodeId ng_count_node;

    /* Methods. */
    UA_NodeId start_method;
    UA_NodeId complete_method;
    UA_NodeId reset_method;

    /* Custom event types (subtypes of BaseEventType). */
    UA_NodeId inspection_event_type;
    UA_NodeId ng_event_type;
    UA_NodeId completion_event_type;

    /* Mutable process state. Work order/status are read back from the server
     * nodes so direct client writes remain authoritative. */
    UA_UInt32 target_count;
    UA_UInt32 inspect_count;
    UA_UInt32 ok_count;
    UA_UInt32 ng_count;
    UA_Boolean ng_window[QC_NG_WINDOW_SIZE];
    size_t ng_window_size;
    size_t ng_window_head;
} QCStationModel;

/* Build the complete QCStation address space and register the method callbacks.
 * Returns a non-GOOD status if any node, event type, or method cannot be added. */
UA_StatusCode qc_model_init(QCStationModel *model, UA_Server *server,
                            UA_UInt16 namespace_index, QCIsAdminFn is_admin);

/* Release NodeIds owned by the model. The UA_Server must still be alive when
 * this is called. */
void qc_model_clear(QCStationModel *model);

/* Record one inspection result and emit InspectionFinishedEventType, plus
 * NgDetectedEventType for an NG result. This is the C-side replacement for the
 * Python record_inspection() hook and is intended to be called by the real
 * vision/AI pipeline when the station is Running. */
UA_StatusCode qc_model_record_inspection(QCStationModel *model,
                                         UA_Boolean is_ok,
                                         const char *defect_type,
                                         double confidence,
                                         const char *image_id,
                                         const char *serial_number);

/* Zero counters and the NG window, then publish NgRate=-1.0. */
UA_StatusCode qc_model_reset_counters(QCStationModel *model);

#ifdef __cplusplus
}
#endif

#endif /* QC_MODEL_H_ */
