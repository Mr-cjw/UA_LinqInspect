# Hardware Migration Architecture

Status: design and reference implementation skeleton. The Python server remains the
behavioral reference until the C server completes integration and acceptance testing.

## 1. Goals and Non-Goals

The hardware track moves the existing software-only OPC UA prototype onto a
Raspberry Pi 5 edge server with STM32 field nodes. It must preserve the existing
behavioral contract rather than invent a second, incompatible plant model.

Goals:

- Run the QC address space on a Raspberry Pi 5 using C open62541.
- Keep only `Basic256Sha256` with `SignAndEncrypt` on the wire.
- Keep CA-based certificate validation and fail closed when security material is
  absent or invalid.
- Keep anonymous sessions read-only, including anonymous sessions on a
  certificate-validated secure channel.
- Keep write and method-call authority exclusively behind an authenticated
  username/password session.
- Connect constrained STM32F407/H743 field nodes as OPC UA clients over FreeRTOS
  and lwIP.
- Leave the Python server, Node-RED flows, and test suite unchanged during this
  migration phase.

Non-goals:

- Replacing the Python reference implementation in this phase.
- Server-to-client method invocation. OPC UA Methods are called by a client
  against a server; the server cannot push a Method Call into a client.
- Certified functional-safety, SIL, or IEC 62443 compliance. The design provides
  the engineering controls and evidence points needed for a later assessment, not
  a certification claim.

## 2. Target Topology

```text
                         Plant / OT VLAN (controlled, firewalled)

  +-------------------+        opc.tcp 4840        +----------------------+
  |  Raspberry Pi 5   |<----------------------------|  Node-RED / MES GW   |
  |  Edge OPC UA      |    Basic256Sha256           |  OPC UA client       |
  |  Server           |    SignAndEncrypt           |  + HTTP MES adapter  |
  |                   |                             +----------+-----------+
  |  QCStation model  |                                        |
  |  PKI + audit log  |                                        | HTTPS/JSON
  +---------+---------+                                        v
            |                                           +----------------+
            | opc.tcp 4840, encrypted + authenticated    |  MES / historian|
            |                                           +----------------+
   +--------+---------+        +-------------------+
   | STM32F407/H743   |        | STM32F407/H743    |
   | Field node 01    |        | Field node 02     |
   | FreeRTOS + lwIP  |        | FreeRTOS + lwIP   |
   | OPC UA client    |        | OPC UA client     |
   +------------------+        +-------------------+
          | sensor / actuator           | sensor / actuator
          v                             v
     Temperature, motor,            Temperature, motor,
     vision trigger, stack light   vision trigger, stack light
```

Deployment rules:

- Put the Pi, STM32 nodes, and Node-RED on a dedicated industrial VLAN or a
  physically isolated switch. Do not expose port 4840 to the corporate network or
  the Internet directly.
- If remote access is required, terminate it in a managed VPN/OT gateway. Do not
  publish OPC UA through a raw port forward.
- Use a managed switch with per-port VLAN and storm protection. Maintain a spare
  configuration backup for the switch.
- Use a 24 V DIN-rail supply with surge/EMC protection for field hardware; the Pi
  itself needs a regulated 5 V/5 A supply and a UPS or orderly-shutdown supervisor.
- Keep certificate and private-key provisioning off the plant VLAN whenever
  possible. Provision from a maintenance network or removable media, then verify
  file permissions before starting the service.

### 2.1 Roles and Interfaces

| Component | OPC UA role | Responsibility | Network |
| --- | --- | --- | --- |
| Raspberry Pi 5 | Server | QCStation address space, Methods, Events, session policy, certificate validation, audit/event emission | OT VLAN, TCP 4840 |
| Node-RED | Client | MES orchestration, subscriptions, write-backs | OT VLAN to Pi |
| STM32 field node | Client | Read command mailbox, write sensor/actuator values, acknowledge commands | OT VLAN to Pi |
| UaExpert / engineering client | Client | Commissioning and diagnostics only | Maintenance VLAN / temporary |

The Pi is not a transparent protocol bridge. It is the authoritative OPC UA
server for the station and the trust boundary for write/call authorization.

### 2.2 Command Semantics for Field Nodes

Because an OPC UA server cannot invoke a method on a connected client, field
commands must use one of these patterns:

1. **Command mailbox (recommended for constrained nodes):** the Pi exposes
   variables such as `QCStation.FieldNodes.STM32_01.Command.StopMotor` and
   `Command.Sequence`. The STM32 reads the mailbox, executes idempotently, and
   writes an acknowledgement (`CommandAck`, `CommandSequence`, `CommandResult`).
2. **Client-pull method:** the STM32 calls a server Method such as
   `GetNextCommand()` and receives a command token. The same method call returns
   `BadNoData`/an empty command when no work is queued.

The mailbox pattern is preferred for F407-class devices because it avoids a
server method call in the command path and makes retry/idempotency explicit.
Every command needs a monotonically increasing sequence number; a repeated
sequence means "execute once, acknowledge again", never "execute twice".

## 3. Address Space Compatibility

The C server must reproduce the Python namespace and NodeId convention:

- Namespace URI: `urn:fde:linqinspect:qc`
- NodeIds: `ns=<index>;s=<readable-name>` (normally index 2)
- Root: `QCStation`
- Children: `Inspection`, `Statistics`, `Events`
- Variables: `DeviceStatus`, `CurrentWorkOrderId`, `CurrentPartSN`,
  `LastMesOrderNo`, `LastNgMesOrderNo`, `LastCompletionReceiptNo`
- Methods: `StartInspection(String, UInt32)`, `CompleteWorkOrder(String)`,
  `ResetCounters()`
- Event notifier: `QCStation.Events`
- Event types: `InspectionFinishedEventType`, `NgDetectedEventType`,
  `WorkOrderCompletedEventType`, each a subtype of `BaseEventType`

Behavioral invariants to port unchanged:

- `DeviceStatus`: `0=Idle`, `1=Running`, `2=Fault`
- Empty NG window reports `NgRate = -1.0`, never `0.0`
- `StartInspection` rejects an empty work order, `Fault`, or target `<= 0`.
  Repeating the active work order is idempotently accepted; a different active
  work order is rejected.
- `CompleteWorkOrder` requires the matching active work order and `Running`
  state, emits the completion event, then returns to `Idle`.
- `ResetCounters` zeroes counters and the window and restores `NgRate = -1.0`.

The C skeleton in `raspberry_pi_server/qc_model.c` implements this core tree and
business validation. Production-specific additions such as `FieldNodes` should
be added in a separate namespace or as an explicit extension, not by silently
renaming frozen QCStation nodes.

## 4. Security Architecture

### 4.1 Trust and Session Model

Trust is split into two independent decisions:

1. **Secure-channel trust:** the client certificate must chain to the configured
   local CA and must be valid at the current time. This establishes an encrypted,
   integrity-protected channel and identifies the device for transport policy.
2. **User authorization:** a session is read-write only after a successful
   username/password login against `UA_ADMIN_USER` / `UA_ADMIN_PASS`.

A trusted client certificate is never a substitute for a user login. Anonymous
sessions remain read-only even when the peer certificate is trusted. Certificate
identity tokens are not accepted as an authorization source by the C skeleton;
only anonymous and encrypted username tokens are advertised.

Anonymous sessions may:

- Browse the address space.
- Read variables.
- Subscribe to data changes and events.

Anonymous sessions may not:

- Write any variable.
- Call `StartInspection`, `CompleteWorkOrder`, or `ResetCounters`.
- Add/delete nodes or references.

### 4.2 Reusing the Existing Local CA

The existing `scripts/setup_pki.py` already creates a Root CA, a server leaf, and
one client leaf. Reuse it with a hardware-specific output directory so the
software prototype certificates are not overwritten:

```bash
cd /home/xunmengtong/UA_LinqInspect
.venv/bin/python scripts/setup_pki.py \
  --out certs-hardware \
  --hostname rpi5-qc.local \
  --server-uri urn:fde:linqinspect:edge:pi5 \
  --client-uri urn:fde:linqinspect:field:stm32
```

This creates:

```text
certs-hardware/
├── ca/                  Root CA key, cert, CRL
├── server/              Pi server cert/key + CA + CRL
├── client/              STM32 client cert/key + CA + CRL
└── trusted_clients/     CA cert for server-side client validation
```

Important PKI rules:

- The server application URI must match the certificate SAN URI. Set
  `UA_APPLICATION_URI=urn:fde:linqinspect:edge:pi5` on the Pi, matching the
  `--server-uri` value used above.
- The client application URI must match the client certificate SAN URI. The
  STM32 firmware must send `urn:fde:linqinspect:field:stm32`.
- Install the CA certificate and CRL in the Pi trust directory. The C skeleton
  loads flat `.der`/`.pem` files and classifies names containing `crl` as
  revocation material.
- The script currently issues one client identity per run. Do **not** create a
  second STM32 identity by running the script with a new `--out`: that creates a
  second Root CA and the Pi will not trust it. For multiple STM32 nodes, issue
  additional client leaves from the same `certs-hardware/ca/ca_key.pem` (for
  example with an internal certificate helper), or keep one device identity for
  the first pilot and add a proper per-device issuance workflow before scale-out.
- The script's client Common Name is generic (`UA LinqInspect Node-RED Client`).
  OPC UA validation keys on the application URI/SAN and chain, not that label.
  Rename the identity in a future issuance helper if operational clarity matters.

Suggested Pi provisioning layout:

```text
/etc/ua-linqinspect/pki/
├── server_cert.der
├── server_key.pem          chmod 600, owned by the service account
└── trusted/
    ├── ca_cert.der
    └── ca_crl.der
```

Suggested STM32 provisioning:

```text
client_cert.der             public, may live in read-only flash
client_key.pem              device-unique, encrypted/access-controlled storage
ca_cert.der                 trusted server/CA material
ca_crl.der                  current CRL, if available
```

Never reuse one private key across all STM32 devices. On F407/H743 hardware
without a secure element, at minimum use a unique key per device, disable debug
access in production, and protect the key with the MCU readout protection and an
encrypted/authenticated configuration store. A secure element or TPM is strongly
recommended for any pilot that leaves the lab.

### 4.3 Certificate Lifecycle and Time

- Certificates are time-valid. A Pi without a correct clock will reject valid
  peer certificates; an STM32 without RTC/SNTP may do the same.
- Synchronize the Pi with an internal NTP source and monitor clock offset.
- Give every STM32 an RTC backed by a coin cell or supercapacitor and synchronize
  it with SNTP after network bring-up. Do not start a secure session before the
  clock is considered valid.
- Rotate server and client certificates before expiry. The Python server warns at
  30 days; mirror that monitoring on the Pi and add an external alert.
- Publish and distribute CRLs with each rotation. A stale CRL is a security
  finding, not just an operational warning.
- Secure-channel lifetime and session timeout should be shorter than certificate
  remaining validity plus the maintenance window.

### 4.4 Failure Policy

Startup must fail closed if any of the following is true:

- Server certificate or private key is missing, unreadable, empty, or mismatched.
- Trust directory is missing or contains no CA certificate.
- Trust material cannot be parsed.
- The server cannot add `Basic256Sha256` or create its `SignAndEncrypt` endpoint.
- Admin credentials are absent while write/call access is expected.

Never enable `SecurityPolicy#None`, never silently downgrade, and never accept all
certificates as a recovery shortcut. A misconfiguration must produce a clear
startup error and a stopped service.

## 5. open62541 Build Trimming

### 5.1 Raspberry Pi 5 Server

The Pi 5 has ample resources for the full QCStation model, subscriptions,
events, and Node-RED clients. Build open62541 1.4.x with mbedTLS and keep the
namespace at `REDUCED` or `FULL` depending on the final node set. `REDUCED` is
sufficient for the QC skeleton; use `FULL` only if a client requires the complete
standard namespace.

Representative build switches:

```text
-DUA_ENABLE_ENCRYPTION=MBEDTLS
-DUA_ENABLE_METHODCALLS=ON
-DUA_ENABLE_SUBSCRIPTIONS=ON
-DUA_ENABLE_SUBSCRIPTIONS_EVENTS=ON
-DUA_ENABLE_DISCOVERY=OFF              # unless LDS is required
-DUA_ENABLE_DISCOVERY_MULTICAST=OFF
-DUA_ENABLE_HISTORIZING=OFF
-DUA_ENABLE_PUBSUB=OFF
-DUA_ENABLE_PUBSUB_INFORMATIONMODEL=OFF
-DUA_ENABLE_JSON_ENCODING=OFF
-DUA_ENABLE_DIAGNOSTICS=OFF
-DUA_NAMESPACE_ZERO=REDUCED
-DUA_ENABLE_AMALGAMATION=OFF
```

Do not disable methods, subscriptions, or events on the server: the MES gateway
and QC contract depend on them.

### 5.2 STM32 Memory Budget

There is no universal "Basic256Sha256 fits in X KB" number. The stack size
depends on the open62541 version, mbedTLS configuration, endpoint count, message
size, subscriptions, and allocator behavior. Measure with the target toolchain
and a static high-water-mark probe.

Practical guidance:

- **STM32H743 (1 MB SRAM, 2 MB flash):** preferred target. It can host
  open62541, FreeRTOS, lwIP, mbedTLS, and application buffers with reasonable
  headroom for `Basic256Sha256` SignAndEncrypt.
- **STM32F407 (192 KB SRAM):** proof-of-concept only. Treat it as marginal for a
  full secure OPC UA client. Use `UA_NAMESPACE_ZERO=MINIMAL`, no subscriptions
  unless required, a small send/receive message size, static/arena allocation,
  and a trimmed mbedTLS configuration. Do not promise production headroom on
  F407 without measured evidence.
- If the measured worst-case stack plus heap exceeds roughly 70% of SRAM, move to
  H743 or reduce scope. Leave memory for lwIP pbufs, FreeRTOS tasks/stacks,
  diagnostics, and watchdog recovery.

### 5.3 STM32 open62541 Version and Port

open62541 1.4.x targets POSIX/Windows in the upstream tree and does not ship the
old `arch/freertosLWIP` port. Use open62541 **1.3.10** (or another pinned 1.3.x
release) for the STM32 FreeRTOS/lwIP port, or maintain a reviewed vendor port
against 1.4.x. Do not mix a Pi built against 1.4 APIs with an STM32 build whose
port was written for a different major source layout without an integration test.

Representative STM32 switches (open62541 1.3.x):

```text
-DUA_ARCHITECTURE=freertosLWIP
-DUA_ENABLE_ENCRYPTION=MBEDTLS
-DUA_ENABLE_METHODCALLS=ON
-DUA_ENABLE_SUBSCRIPTIONS=OFF          # ON only if the command/telemetry design needs it
-DUA_ENABLE_SUBSCRIPTIONS_EVENTS=OFF   # no event subscription on constrained node
-DUA_ENABLE_DISCOVERY=OFF
-DUA_ENABLE_HISTORIZING=OFF
-DUA_ENABLE_PUBSUB=OFF
-DUA_ENABLE_PUBSUB_INFORMATIONMODEL=OFF
-DUA_ENABLE_JSON_ENCODING=OFF
-DUA_ENABLE_NODEMANAGEMENT=OFF         # client-only build does not add nodes
-DUA_ENABLE_STATUSCODE_DESCRIPTIONS=OFF
-DUA_ENABLE_TYPEDESCRIPTION=OFF
-DUA_NAMESPACE_ZERO=MINIMAL
-DUA_ENABLE_AMALGAMATION=ON            # convenient for CubeIDE integration
```

For mbedTLS, keep only the cryptography and X.509 primitives needed by
open62541: RSA, SHA-256, AES, ASN.1, X.509 parsing/verification, and the platform
abstraction. Disable TLS/DTLS protocol code, unused cipher suites, test hooks,
and entropy sources that do not apply to the MCU. Provide a hardware RNG or a
properly seeded platform entropy source; do not ship a deterministic RNG.

If static allocation is required, use a custom arena through the open62541
allocation hooks and an mbedTLS allocator backed by the same preallocated region.
Set a hard cap and fail startup if the arena cannot satisfy the initial secure
client configuration. Exhaustion at runtime must trigger a controlled reconnect,
not a random crash.

## 6. Reliability and Operations

- Run the Pi server under systemd with `Restart=on-failure`, a dedicated
  unprivileged service account, `NoNewPrivileges=yes`, `PrivateTmp=yes`, and a
  read-only root filesystem where practical.
- Put the PKI directory outside the writable application directory and mount it
  read-only to the service.
- Use a watchdog for the Pi application process and an independent hardware
  watchdog for each STM32.
- STM32 reconnect policy: exponential backoff with jitter, finite connect
  timeout, certificate/session teardown on failure, and a bounded retry cycle.
  Do not hot-loop reconnects.
- Field command handling must be idempotent and sequence-numbered. A reconnect
  after a power loss must not repeat a motor action.
- Record OPC UA session activation failures, method calls, write-backs, and event
  generations in a tamper-resistant audit stream on the Pi. The Python server's
  audit logger is the behavioral reference; do not log passwords or private keys.
- Monitor disk space for audit logs, certificate expiry, clock offset, secure
  channel failures, and STM32 last-seen time.

## 7. Migration Phases

1. **Reference parity:** build the Pi C server on a bench with the existing
   certs-hardware CA, validate the QCStation tree and methods against the Python
   contract, and run Node-RED against it.
2. **Lab field node:** connect one H743 (preferred) or F407 with a device-unique
   certificate, prove anonymous read-only versus admin read-write, and verify
   command idempotency across reconnect and power-cycle.
3. **Hardening:** add CRL rotation, audit shipping, systemd sandboxing, SNTP,
   watchdog tests, and a denial test with an untrusted certificate.
4. **Pilot:** deploy on an isolated OT VLAN with a managed switch, UPS, spare
   hardware, documented rollback, and a measured memory/latency report from the
   actual target.

The C skeleton is intentionally conservative and reference-oriented. Do not call
the hardware migration complete until the four security cases (anonymous read,
anonymous write denial, anonymous method denial, admin write/call success) have
been executed against the built binary and the target certificates.
