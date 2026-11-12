# STM32 Field Node Client

This directory documents the field-node side of the hardware migration. It is a
design and pseudo-code reference, not a drop-in CubeIDE project. The generated
code in `raspberry_pi_server/` is the Pi-side skeleton; the STM32 integration
must be built against the actual board support package and network stack in use.

## 1. Target Selection and Scope

- Preferred production target: STM32H743 (Cortex-M7, 1 MB SRAM, 2 MB flash).
- Proof-of-concept target: STM32F407 (Cortex-M4, 192 KB SRAM). This is marginal
  for an encrypted OPC UA client and must be measured before any pilot claim.
- RTOS: FreeRTOS.
- Network: lwIP with a working TCP stack and DNS or static addressing.
- OPC UA: open62541 1.3.x with the `freertosLWIP` architecture port. open62541
  1.4.x does not ship that port upstream; do not assume the Pi 1.4 build can be
  copied into the STM32 project.

The STM32 is an OPC UA **client**. It connects to the Pi, reads command state,
writes telemetry, and calls server methods. The Pi cannot push a Method Call to a
connected client. Any command delivery design must respect that direction.

## 2. Command Model

### Recommended: sequence-numbered command mailbox

The Pi exposes a small command area per field node:

```text
QCStation
└── FieldNodes
    └── STM32_01
        ├── Temperature        Double, ReadWrite
        ├── MotorRunning       Boolean, ReadWrite
        ├── Command
        │   ├── Sequence        UInt32, ReadWrite
        │   ├── StopMotor       Boolean, ReadWrite
        │   └── Acknowledge     Boolean, ReadWrite
        └── CommandAck
            ├── Sequence        UInt32, ReadWrite
            ├── Result          String, ReadWrite
            └── Timestamp       DateTime, ReadWrite
```

The MES/operator sets a new `Sequence` and `StopMotor=true`. The STM32:

1. Reads `Sequence` and `StopMotor` (or subscribes if RAM allows).
2. If `Sequence` is new, performs the stop exactly once.
3. Writes `CommandAck.Sequence`, `CommandAck.Result`, and a timestamp.
4. Treats a repeated sequence as "execute once, acknowledge again".

This is the preferred pattern for F407/H743 because it survives reconnect and
power-cycle without relying on a server-side call into the client.

### Alternative: client-pull Method

The Pi can expose `GetNextCommand()` returning
`(Boolean HasCommand, UInt32 Sequence, String Command)`. The STM32 calls it on a
polling interval and then acknowledges the command. The method is an extension
to the current QCStation tree and must be added to the C model before use.

Do not design a method as a fire-and-forget motor action without an
acknowledgement and sequence number. A network retry must not become a second
physical action.

## 3. open62541 + FreeRTOS + lwIP Integration

### 3.1 lwIP configuration

The open62541 FreeRTOS/lwIP port expects the socket API and DNS. Start with these
settings in `lwipopts.h`, then tune them against the measured memory budget:

```c
#define LWIP_COMPAT_SOCKETS       0
#define LWIP_SOCKET               1
#define LWIP_DNS                  1
#define SO_REUSE                  1
#define LWIP_TIMEVAL_PRIVATE      0

/* Sizes are examples, not universal values. Measure with your build. */
#define MEM_SIZE                  (32 * 1024)
#define MEMP_NUM_TCP_PCB          8
#define TCP_MSS                   1460
#define TCP_SND_BUF               (4 * TCP_MSS)
#define TCP_WND                   (4 * TCP_MSS)
```

Do not enable lwIP features that are not needed by the OPC UA client. Keep the
TCP PCB and pbuf pools bounded so a reconnect storm cannot exhaust the heap.

### 3.2 FreeRTOS task model

- One dedicated OPC UA task owns the client object and all client calls.
- Start it only after the interface has a link, an IP address, and a valid clock.
- Give it a fixed stack sized from the measured high-water mark. Start with a
  conservative 24-32 KB on H743; do not assume F407 can provide that alongside
  lwIP and mbedTLS.
- Do not call `UA_Client_*` from multiple tasks unless the open62541
  multithreading configuration and locking are explicitly validated.
- Set a task watchdog checkpoint around `UA_Client_run_iterate` and command
  processing. A blocked network call must eventually trigger recovery.
- Put the I/O task at a priority that leaves the network/lwIP task able to run.

### 3.3 Event loop and reconnect

```c
static void opcua_task(void *argument) {
    (void)argument;
    uint32_t backoff_ms = 1000;

    for(;;) {
        if(client == NULL) {
            client = secure_client_create();
            if(client == NULL || ua_connect_secure(client) != UA_STATUSCODE_GOOD) {
                secure_client_destroy(&client);
                vTaskDelay(pdMS_TO_TICKS(backoff_ms));
                backoff_ms = MIN(backoff_ms * 2u, 30000u);
                continue;
            }
            backoff_ms = 1000;
            ua_resubscribe_or_resync(client);
        }

        UA_StatusCode rc = UA_Client_run_iterate(client, 100);
        if(rc != UA_STATUSCODE_GOOD) {
            secure_client_destroy(&client);
            vTaskDelay(pdMS_TO_TICKS(backoff_ms));
            backoff_ms = MIN(backoff_ms * 2u, 30000u);
            continue;
        }

        field_process_commands(client);
        field_sample_and_write(client);
        vTaskDelay(pdMS_TO_TICKS(100));
    }
}
```

Add random jitter to the backoff if several field nodes may reconnect at once.
After reconnect, resynchronize the command sequence and read the current server
state before writing telemetry or acknowledging anything.

## 4. Certificate and Time Provisioning

Generate the hardware PKI with the existing script from the repository root:

```bash
.venv/bin/python scripts/setup_pki.py \
  --out certs-hardware \
  --hostname rpi5-qc.local \
  --server-uri urn:fde:linqinspect:edge:pi5 \
  --client-uri urn:fde:linqinspect:field:stm32
```

Provision the STM32 with:

```text
certs-hardware/client/client_cert.der
certs-hardware/client/client_key.pem
certs-hardware/client/ca_cert.der
certs-hardware/client/ca_crl.der
```

For additional STM32 devices, issue additional client certificates from the same
Root CA. Do not run `setup_pki.py` with a new output directory for each node: that
creates a new CA and the Pi will reject it.

Certificate validation requires a correct wall clock. The raw FreeRTOS tick
counter is not a wall clock. Implement the platform time functions used by
open62541 (or the port's clock hook) so they return RTC/SNTP UTC time, not just
milliseconds since boot. Sync with SNTP after networking starts and do not begin
the secure connection until the time is plausible. A valid RTC backup domain is
recommended.

Private-key storage requirements:

- Use a unique key per device. Never clone one key across a fleet.
- Prefer a secure element/TPM. If unavailable, use readout protection plus an
  encrypted, authenticated configuration store.
- Disable production debug access and remove factory test keys.
- Do not put the private key in source control or in a world-readable filesystem.

## 5. Secure Client Skeleton

The following pseudo-code follows the open62541 1.3 client API shape. Replace the
file-loading functions with your flash/secure-storage reader and replace the
application-specific node IDs with the final `FieldNodes` mapping.

```c
#include <open62541/client_config_default.h>
#include <open62541/client_highlevel.h>
#include <open62541/plugin/securitypolicy.h>

#define QC_APP_URI  "urn:fde:linqinspect:field:stm32"
#define QC_POLICY   "http://opcfoundation.org/UA/SecurityPolicy#Basic256Sha256"

static UA_Client *secure_client_create(void) {
    UA_Client *client = UA_Client_new();
    if(!client)
        return NULL;

    UA_ClientConfig *cc = UA_Client_getConfig(client);

    UA_ByteString cert = secure_store_load("client_cert.der");
    UA_ByteString key  = secure_store_load("client_key.pem");
    UA_ByteString ca   = secure_store_load("ca_cert.der");
    UA_ByteString crl  = secure_store_load("ca_crl.der");
    UA_ByteString trust[1] = { ca };
    UA_ByteString revocation[1] = { crl };

    UA_ClientConfig_setDefaultEncryption(cc, cert, key,
                                         trust, 1,
                                         revocation, 1);

    /* The application URI must match the client certificate SAN URI. */
    UA_String_clear(&cc->clientDescription.applicationUri);
    cc->clientDescription.applicationUri = UA_STRING_ALLOC(QC_APP_URI);
    cc->securityMode = UA_MESSAGESECURITYMODE_SIGNANDENCRYPT;
    cc->securityPolicyUri = UA_STRING_ALLOC(QC_POLICY);

    wipe_and_free(&cert);
    wipe_and_free(&key);
    /* The client config copied the trust material; release local copies. */
    UA_ByteString_clear(&ca);
    UA_ByteString_clear(&crl);
    return client;
}

static UA_StatusCode ua_connect_secure(UA_Client *client) {
    return UA_Client_connectUsername(client,
                                     "opc.tcp://rpi5-qc.local:4840",
                                     UA_ADMIN_USER,
                                     UA_ADMIN_PASS);
}
```

Use the actual deployed credentials in device provisioning, not a compiled-in
string. For the current prototype the only read-write account is
`UA_ADMIN_USER`; production should introduce per-device service accounts with
least privilege instead of sharing one administrator password across every STM32.

## 6. Reading a Method Instruction

OPC UA Methods are invoked by a client. To "read a Method instruction" the STM32
calls a command-fetch Method on the Pi, for example a future extension:

```c
/* Future server extension: GetNextCommand() -> HasCommand, Sequence, Command */
static UA_StatusCode field_get_next_command(UA_Client *client,
                                            UA_UInt16 ns,
                                            field_command_t *out) {
    UA_NodeId object = UA_NODEID_STRING(ns, "QCStation.FieldNodes.STM32_01");
    UA_NodeId method = UA_NODEID_STRING(ns, "QCStation.FieldNodes.STM32_01.GetNextCommand");

    size_t output_size = 0;
    UA_Variant *output = NULL;
    UA_StatusCode rc = UA_Client_call(client, object, method, 0, NULL,
                                      &output_size, &output);
    if(rc != UA_STATUSCODE_GOOD)
        return rc;

    UA_Boolean has_command = UA_FALSE;
    UA_UInt32 sequence = 0;
    UA_String command = UA_STRING_NULL;

    if(output_size >= 3 &&
       UA_Variant_hasScalarType(&output[0], &UA_TYPES[UA_TYPES_BOOLEAN]) &&
       UA_Variant_hasScalarType(&output[1], &UA_TYPES[UA_TYPES_UINT32]) &&
       UA_Variant_hasScalarType(&output[2], &UA_TYPES[UA_TYPES_STRING])) {
        has_command = *(UA_Boolean*)output[0].data;
        sequence = *(UA_UInt32*)output[1].data;
        command = *(UA_String*)output[2].data;
        copy_to_local(out, has_command, sequence, &command);
    } else {
        rc = UA_STATUSCODE_BADTYPEMISMATCH;
    }

    UA_Array_delete(output, output_size, &UA_TYPES[UA_TYPES_VARIANT]);
    return rc;
}
```

For the mailbox pattern, read the command variables directly:

```c
static UA_StatusCode field_read_stop_command(UA_Client *client, UA_UInt16 ns,
                                             UA_UInt32 *sequence,
                                             UA_Boolean *stop_motor) {
    UA_NodeId seq_node = UA_NODEID_STRING(
        ns, "QCStation.FieldNodes.STM32_01.Command.Sequence");
    UA_NodeId cmd_node = UA_NODEID_STRING(
        ns, "QCStation.FieldNodes.STM32_01.Command.StopMotor");

    UA_Variant seq_value;
    UA_Variant cmd_value;
    UA_Variant_init(&seq_value);
    UA_Variant_init(&cmd_value);

    UA_StatusCode rc = UA_Client_readValueAttribute(client, seq_node, &seq_value);
    if(rc == UA_STATUSCODE_GOOD)
        rc = UA_Client_readValueAttribute(client, cmd_node, &cmd_value);
    if(rc == UA_STATUSCODE_GOOD &&
       UA_Variant_hasScalarType(&seq_value, &UA_TYPES[UA_TYPES_UINT32]) &&
       UA_Variant_hasScalarType(&cmd_value, &UA_TYPES[UA_TYPES_BOOLEAN])) {
        *sequence = *(UA_UInt32*)seq_value.data;
        *stop_motor = *(UA_Boolean*)cmd_value.data;
    } else {
        rc = UA_STATUSCODE_BADTYPEMISMATCH;
    }
    UA_Variant_clear(&seq_value);
    UA_Variant_clear(&cmd_value);
    return rc;
}
```

Execute `StopMotor` only if `sequence` is newer than the last executed sequence.
Write the acknowledgement only after the physical action has reached a safe
state.

## 7. Writing Sensor Data

```c
static UA_StatusCode field_write_temperature(UA_Client *client, UA_UInt16 ns,
                                             double temperature_c) {
    UA_NodeId node = UA_NODEID_STRING(
        ns, "QCStation.FieldNodes.STM32_01.Temperature");

    UA_Variant value;
    UA_Variant_init(&value);
    UA_Variant_setScalar(&value, &temperature_c, &UA_TYPES[UA_TYPES_DOUBLE]);

    UA_StatusCode rc = UA_Client_writeValueAttribute(client, node, &value);
    UA_Variant_clear(&value);
    return rc;
}
```

The client must authenticate with a read-write account for this write. Anonymous
sessions are intentionally read-only even when their client certificate is
trusted. The current prototype's admin account is the only read-write account;
that is a pilot limitation, not a production role model.

## 8. Memory and Performance Checklist

- [ ] Build open62541 1.3.10 with `UA_ARCHITECTURE=freertosLWIP` and mbedTLS.
- [ ] Measure `.text`, `.data`, `.bss`, FreeRTOS heap, lwIP pools, and mbedTLS
      heap usage on the actual board, not on a host build.
- [ ] Record worst-case OPC UA task stack high-water mark during connect,
      encrypt/decrypt, method call, write, reconnect, and certificate failure.
- [ ] Verify no malloc failure occurs during a reconnect storm or a malformed
      packet. A failed allocation must close the session and back off.
- [ ] Use `UA_NAMESPACE_ZERO=MINIMAL`, `UA_ENABLE_SUBSCRIPTIONS=OFF` (unless
      specifically required), `UA_ENABLE_DISCOVERY=OFF`,
      `UA_ENABLE_PUBSUB_INFORMATIONMODEL=OFF`, and no PubSub/historizing.
- [ ] Keep messages below the configured buffer limits and reject oversize input
      cleanly.
- [ ] Run a 24-hour soak with repeated Pi restarts, cable pulls, and certificate
      rotation.

## 9. Known Limitations

- The current server has one global admin username/password and no per-device
  role model. All write-capable STM32 nodes therefore share that authority.
- The current PKI script issues one client leaf per invocation. Multi-node
  issuance from the same CA requires an additional issuance helper before pilot
  scale-out.
- The STM32 pseudo-code is not compiled in this repository. It must be adapted to
  the exact FreeRTOS/lwIP/BSP versions and validated with the target toolchain.
- F407 memory is a real constraint. Do not promote an F407 prototype to a
  production claim without measured memory and soak-test evidence.
