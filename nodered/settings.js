'use strict';

const fs = require('node:fs');
const path = require('node:path');

function resolveProjectModule(name) {
  const candidates = [
    process.env.QC_REPO_ROOT && path.join(process.env.QC_REPO_ROOT, 'nodered', name),
    path.join(process.cwd(), 'nodered', name),
    path.join(__dirname, '..', 'nodered', name),
  ].filter(Boolean);
  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) return candidate;
  }
  throw new Error(`Cannot locate nodered/${name}; set QC_REPO_ROOT to the repository root`);
}

const storeModule = require(resolveProjectModule('sqlite_store.js'));
const queueLogic = require(resolveProjectModule('queue_logic.js'));

module.exports = {
  flowFile: 'flows.json',
  credentialSecret: false,
  contextStorage: {
    default: {
      module: 'localfilesystem',
      config: {
        cache: true,
        flushInterval: 1,
      },
    },
  },
  functionGlobalContext: {
    qstore: storeModule.createStore(),
    qlogic: queueLogic,
  },
};
