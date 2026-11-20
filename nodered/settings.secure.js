/*
 * Secure Node-RED settings baseline for UA_LinqInspect.
 *
 * Install with:
 *   .venv/bin/python scripts/setup_node_red_security.py
 *
 * Required environment variables at Node-RED startup:
 *   NODERED_ADMIN_USER
 *   NODERED_ADMIN_PASSWORD
 *   NODERED_CREDENTIAL_SECRET
 *   NODERED_HTTP_USER
 *   NODERED_HTTP_PASS
 *   QC_REPO_ROOT
 *   QC_DB_PATH
 */
'use strict';

const fs = require('node:fs');
const path = require('node:path');
const bcrypt = require('bcryptjs');

const adminUser = process.env.NODERED_ADMIN_USER || '';
const adminPassword = process.env.NODERED_ADMIN_PASSWORD || '';
const credentialSecret = process.env.NODERED_CREDENTIAL_SECRET || '';
const httpUser = process.env.NODERED_HTTP_USER || '';
const httpPassword = process.env.NODERED_HTTP_PASS || '';
const repoRoot = process.env.QC_REPO_ROOT || path.join(__dirname, '..');

if (!adminUser || !adminPassword || !credentialSecret) {
    throw new Error(
        'NODERED_ADMIN_USER, NODERED_ADMIN_PASSWORD and NODERED_CREDENTIAL_SECRET must be set'
    );
}
if (!httpUser || !httpPassword) {
    throw new Error('NODERED_HTTP_USER and NODERED_HTTP_PASS must be set');
}
if (!process.env.QC_DB_PATH) {
    throw new Error('QC_DB_PATH must be set');
}

function resolveProjectModule(name) {
    const candidate = path.join(repoRoot, 'nodered', name);
    if (!fs.existsSync(candidate)) {
        throw new Error(`Cannot locate nodered/${name}; check QC_REPO_ROOT`);
    }
    return candidate;
}

const storeModule = require(resolveProjectModule('sqlite_store.js'));
const queueLogic = require(resolveProjectModule('queue_logic.js'));
const passwordHash = bcrypt.hashSync(adminPassword, 8);
const httpPasswordHash = bcrypt.hashSync(httpPassword, 8);

module.exports = {
    uiHost: process.env.NODERED_UI_HOST || '127.0.0.1',
    flowFile: 'flows.json',
    credentialSecret,
    flowFilePretty: true,

    adminAuth: {
        type: 'credentials',
        users: [{
            username: adminUser,
            password: passwordHash,
            permissions: '*'
        }]
    },

    httpNodeAuth: {
        user: httpUser,
        pass: httpPasswordHash
    },

    contextStorage: {
        default: {
            module: 'localfilesystem',
            config: {
                base: 'context',
                cache: true,
                flushInterval: 1
            }
        }
    },

    functionGlobalContext: {
        qstore: storeModule.createStore(),
        qlogic: queueLogic
    },

    logging: {
        console: {
            level: 'info',
            metrics: false,
            audit: false
        }
    },

    editorTheme: {
        projects: {
            enabled: false
        }
    },

    externalModules: {
        palette: {
            allowInstall: false
        }
    }
};
