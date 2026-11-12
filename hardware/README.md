# Hardware Build Guide

This guide builds the Raspberry Pi 5 open62541 server and explains the STM32
cross-build path. The Pi server is the executable reference skeleton in
`hardware/raspberry_pi_server/`. The STM32 side is documented in
`hardware/stm32_client/README.md` and must be integrated with the actual board
support package.

No Python server, Node-RED flow, test, or TODO file is modified by this hardware
track.

## 1. Prerequisites

### Raspberry Pi OS / Ubuntu on Pi 5

```bash
sudo apt-get update
sudo apt-get install -y \
  build-essential cmake git pkg-config \
  libmbedtls-dev
```

Verify the required tools:

```bash
cmake --version
gcc --version
pkg-config --modversion mbedtls
```

If `pkg-config --modversion mbedtls` fails, install the development package for
your distribution (`libmbedtls-dev` on Debian/Ubuntu) before continuing. The
application intentionally fails its build rather than silently linking without
the crypto backend.

## 2. Generate Hardware Certificates

From the repository root, create a separate hardware PKI. Do not overwrite the
Python/Node-RED `certs/` directory used by the software prototype.

```bash
cd /home/xunmengtong/UA_LinqInspect
.venv/bin/python scripts/setup_pki.py \
  --out certs-hardware \
  --hostname rpi5-qc.local \
  --server-uri urn:fde:linqinspect:edge:pi5 \
  --client-uri urn:fde:linqinspect:field:stm32
```

The Pi server will use:

```text
certs-hardware/server/server_cert.der
certs-hardware/server/server_key.pem
certs-hardware/server/ca_cert.der
certs-hardware/server/ca_crl.der
```

The STM32 client will use:

```text
certs-hardware/client/client_cert.der
certs-hardware/client/client_key.pem
certs-hardware/client/ca_cert.der
certs-hardware/client/ca_crl.der
```

Create the Pi runtime trust directory and install the CA plus CRL. The C server
loads a flat directory and treats filenames containing `crl` as revocation
material:

```bash
sudo install -d -m 0750 /etc/ua-linqinspect/pki/trusted
sudo install -m 0644 certs-hardware/server/ca_cert.der \
  /etc/ua-linqinspect/pki/trusted/ca_cert.der
sudo install -m 0644 certs-hardware/server/ca_crl.der \
  /etc/ua-linqinspect/pki/trusted/ca_crl.der
sudo install -m 0644 certs-hardware/server/server_cert.der \
  /etc/ua-linqinspect/pki/server_cert.der
sudo install -m 0600 certs-hardware/server/server_key.pem \
  /etc/ua-linqinspect/pki/server_key.pem
```

The certificate SAN URI must match the runtime application URI. The command above
sets both to `urn:fde:linqinspect:edge:pi5`; the C server checks this at startup
and refuses to start if the certificate does not contain the configured URI.

For multiple STM32 devices, issue additional client leaves from the **same**
`certs-hardware/ca/ca_key.pem`. Running `setup_pki.py --out certs-hardware-02`
would create a second Root CA and is not the correct multi-device workflow.

## 3. Build open62541 for the Pi

The C skeleton is written against open62541 1.4.x API shapes. Pin a specific
release for reproducibility. Example using 1.4.10:

```bash
cd /home/xunmengtong/UA_LinqInspect
git clone --branch v1.4.10 --depth 1 \
  https://github.com/open62541/open62541.git /tmp/open62541-1.4.10

cmake -S /tmp/open62541-1.4.10 -B /tmp/open62541-build \
  -DCMAKE_BUILD_TYPE=RelWithDebInfo \
  -DCMAKE_INSTALL_PREFIX=/usr/local \
  -DUA_ENABLE_ENCRYPTION=MBEDTLS \
  -DUA_ENABLE_METHODCALLS=ON \
  -DUA_ENABLE_SUBSCRIPTIONS=ON \
  -DUA_ENABLE_SUBSCRIPTIONS_EVENTS=ON \
  -DUA_ENABLE_DISCOVERY=OFF \
  -DUA_ENABLE_DISCOVERY_MULTICAST=OFF \
  -DUA_ENABLE_HISTORIZING=OFF \
  -DUA_ENABLE_PUBSUB=OFF \
  -DUA_ENABLE_PUBSUB_INFORMATIONMODEL=OFF \
  -DUA_ENABLE_JSON_ENCODING=OFF \
  -DUA_ENABLE_DIAGNOSTICS=OFF \
  -DUA_NAMESPACE_ZERO=REDUCED \
  -DUA_BUILD_EXAMPLES=OFF \
  -DUA_BUILD_UNIT_TESTS=OFF \
  -DUA_BUILD_TOOLS=OFF

cmake --build /tmp/open62541-build -j"$(nproc)"
sudo cmake --install /tmp/open62541-build
sudo ldconfig
```

If you install to a non-standard prefix, pass it to the application build:

```bash
cmake -DCMAKE_PREFIX_PATH=/opt/open62541 ...
```

## 4. Build the Pi QC Server

```bash
cd /home/xunmengtong/UA_LinqInspect
cmake -S hardware/raspberry_pi_server -B build-pi \
  -DCMAKE_BUILD_TYPE=RelWithDebInfo \
  -DQC_WARNINGS_AS_ERRORS=OFF

cmake --build build-pi -j"$(nproc)"
```

The build links `open62541::open62541` and the mbedTLS libraries. The application
compile guard fails if open62541 was built without encryption, preventing a
silent `NoSecurity` deployment.

## 5. Run the Pi Server

For a bench test bound to loopback:

```bash
export UA_ENDPOINT_PORT=4840
export UA_BIND_ADDRESS=127.0.0.1
export UA_APPLICATION_URI=urn:fde:linqinspect:edge:pi5
export UA_SERVER_CERT=/etc/ua-linqinspect/pki/server_cert.der
export UA_SERVER_PRIVATE_KEY=/etc/ua-linqinspect/pki/server_key.pem
export UA_TRUSTED_CLIENT_CERTS_DIR=/etc/ua-linqinspect/pki/trusted
export UA_ADMIN_USER='XunMengTong'
export UA_ADMIN_PASS='replace-with-the-provisioned-password'

./build-pi/qc_open62541_server
```

For the real OT network, set `UA_BIND_ADDRESS` to the address of the controlled
industrial interface, for example `UA_BIND_ADDRESS=192.168.50.10`. Using
`0.0.0.0` listens on every interface; only use it behind a firewall and a
dedicated OT VLAN. The server never advertises a `SecurityPolicy#None` endpoint.

The process exits with a non-zero status and a clear message when:

- the certificate, private key, trust directory, or admin credentials are
  missing or unreadable;
- the server certificate does not contain the configured application URI;
- the certificate and private key do not form a usable Basic256Sha256 identity;
- the trust directory contains no CA certificate; or
- the Basic256Sha256 SignAndEncrypt endpoint cannot be created.

The server logs a warning if the certificate expires within 30 days.

The skeleton expects an unencrypted DER/PEM private key file and does not
implement `UA_SERVER_PRIVATE_KEY_PASSWORD`. Store the key with mode `0600` and
restrict access to the service account, or extend the loader with an explicit
key-decryption step before calling `UA_ServerConfig_addSecurityPolicyBasic256Sha256`.

## 6. Security Acceptance Cases

Run these against the built Pi binary with a temporary port (for example 4841)
and a temporary trust directory if a production server is already running.

| Case | Client identity | Expected result |
| --- | --- | --- |
| Anonymous secure read | trust CA + client cert, anonymous token | Browse/read succeeds |
| Anonymous secure write | trust CA + client cert, anonymous token | `BadUserAccessDenied` for Write |
| Anonymous secure method | trust CA + client cert, anonymous token | Method is not executable / authorization denied |
| Admin write | valid client cert + username/password | Write succeeds |
| Admin method | valid client cert + username/password | Method reaches business validation |

The C access-control layer denies Method execution before the callback for
anonymous sessions (`BadNotExecutable` may be observed). The callback also checks
the session role and returns `BadUserAccessDenied` as defense in depth if a future
configuration reaches it. Treat either status as an authorization failure; the
Python server's `BadUserAccessDenied` is the behavioral reference.

Use the declared Method argument types exactly. For example, send
`TargetCount` as `UInt32`, not as a generic 64-bit integer; an untyped client
conversion can otherwise produce `BadInvalidArgument` before the business
callback runs.

Do not accept a test that only verifies that `Basic256Sha256` is selected. Test
the four authorization cases with the trusted client certificate present, because
a certificate-validated secure channel must not grant write or method authority.

## 7. Cross-Compile for STM32

The STM32 build is separate from the Pi build and uses open62541 1.3.x, because
the upstream 1.4 tree does not contain the `freertosLWIP` architecture port.
FreeRTOS and lwIP come from the STM32Cube project; open62541 supplies the OPC UA
stack and the port glue.

### 7.1 Toolchain file

Create a toolchain file appropriate for the exact MCU (FPU flags differ between
F407 and H743):

```cmake
# arm-none-eabi-toolchain.cmake
set(CMAKE_SYSTEM_NAME Generic)
set(CMAKE_SYSTEM_PROCESSOR arm)
set(CMAKE_C_COMPILER arm-none-eabi-gcc)
set(CMAKE_CXX_COMPILER arm-none-eabi-g++)
set(CMAKE_ASM_COMPILER arm-none-eabi-gcc)
set(CMAKE_TRY_COMPILE_TARGET_TYPE STATIC_LIBRARY)

set(CMAKE_C_FLAGS_INIT "-mcpu=cortex-m7 -mthumb -mfpu=fpv5-d16 -mfloat-abi=hard -ffunction-sections -fdata-sections")
set(CMAKE_EXE_LINKER_FLAGS_INIT "-Wl,--gc-sections")
```

For STM32F407 use the appropriate Cortex-M4 flags (`-mcpu=cortex-m4`,
`-mfpu=fpv4-sp-d16`). Do not reuse the H743 FPU flags on an F407.

### 7.2 Build open62541 1.3.10 for FreeRTOS/lwIP

```bash
git clone --branch v1.3.10 --depth 1 \
  https://github.com/open62541/open62541.git /tmp/open62541-1.3.10

cmake -S /tmp/open62541-1.3.10 -B /tmp/open62541-stm32 \
  -DCMAKE_TOOLCHAIN_FILE=/path/to/arm-none-eabi-toolchain.cmake \
  -DCMAKE_BUILD_TYPE=MinSizeRel \
  -DUA_ARCHITECTURE=freertosLWIP \
  -DUA_ENABLE_ENCRYPTION=MBEDTLS \
  -DUA_ENABLE_METHODCALLS=ON \
  -DUA_ENABLE_SUBSCRIPTIONS=OFF \
  -DUA_ENABLE_SUBSCRIPTIONS_EVENTS=OFF \
  -DUA_ENABLE_DISCOVERY=OFF \
  -DUA_ENABLE_HISTORIZING=OFF \
  -DUA_ENABLE_PUBSUB=OFF \
  -DUA_ENABLE_PUBSUB_INFORMATIONMODEL=OFF \
  -DUA_ENABLE_JSON_ENCODING=OFF \
  -DUA_ENABLE_NODEMANAGEMENT=OFF \
  -DUA_ENABLE_STATUSCODE_DESCRIPTIONS=OFF \
  -DUA_ENABLE_TYPEDESCRIPTION=OFF \
  -DUA_NAMESPACE_ZERO=MINIMAL \
  -DUA_ENABLE_AMALGAMATION=ON \
  -DUA_BUILD_EXAMPLES=OFF \
  -DUA_BUILD_UNIT_TESTS=OFF \
  -DMBEDTLS_FOLDER_INCLUDE=/path/to/mbedtls/include \
  -DMBEDTLS_FOLDER_LIBRARY=/path/to/stm32-mbedtls/lib

cmake --build /tmp/open62541-stm32 -j"$(nproc)"
```

The lwIP project must define `LWIP_SOCKET=1`, `LWIP_DNS=1`,
`LWIP_COMPAT_SOCKETS=0`, and `SO_REUSE=1` as documented in the STM32 README.
Link the resulting open62541 objects/library with FreeRTOS, lwIP, mbedTLS, and the
STM32 startup/HAL code. The exact object integration belongs in the CubeIDE or
board-level CMake project; this repository does not ship a vendor BSP.

### 7.3 Application integration

Add your STM32 client sources to the same CMake/board project and use the
`field_get_next_command`, command-mailbox read, and `field_write_temperature`
patterns from `stm32_client/README.md`. Keep one OPC UA task, a bounded lwIP pool,
and a watchdog-backed reconnect loop.

## 8. Commissioning Checklist

- [ ] CA, CRL, server certificate, and private key are installed with restrictive
      permissions and are not in a world-writable directory.
- [ ] Server certificate SAN URI equals `UA_APPLICATION_URI`.
- [ ] Only `Basic256Sha256` `SignAndEncrypt` is advertised.
- [ ] Anonymous read, write denial, and method denial are tested with a trusted
      client certificate present.
- [ ] Admin username/password write and method call are tested.
- [ ] Node-RED is pointed at the hardware server endpoint and the new client
      certificate/CA when running the integration test.
- [ ] STM32 RTC/SNTP is valid before the first secure session.
- [ ] Certificate expiry, clock offset, and failed session activations are
      monitored.
- [ ] Pi service is sandboxed (dedicated user, systemd hardening, read-only PKI
      mount) and restarts cleanly after a power cycle.
- [ ] Physical estate: 24 V surge protection, managed switch/VLAN, UPS, and a
      documented recovery procedure are in place.

## 9. What Is Not Built Here

No STM32 BSP, FreeRTOS configuration, lwIP stack, mbedTLS MCU build, Node-RED
flow, or Python test was generated or modified. The Pi C skeleton is intended to
be built and exercised on the target or a Linux bench before it is treated as a
production replacement for the Python server.
