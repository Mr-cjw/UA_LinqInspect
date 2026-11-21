'use strict';

const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

const DEFAULT_LEASE_MS = 10000;

function defaultLockDir() {
  const dataDir = process.env.QC_DATA_DIR
    ? path.resolve(process.env.QC_DATA_DIR)
    : path.join(process.cwd(), 'data');
  return path.resolve(process.env.QC_SENDER_LOCK_DIR || path.join(dataDir, '.sender-authority'));
}

function processAlive(pid) {
  const value = Number(pid);
  if (!Number.isInteger(value) || value <= 0) return false;
  try {
    process.kill(value, 0);
    return true;
  } catch (error) {
    return error && error.code === 'EPERM';
  }
}

class SenderAuthorityGuard {
  constructor(options = {}) {
    this.dir = path.resolve(options.dir || defaultLockDir());
    this.owner = String(options.owner || 'node-red');
    this.leaseMs = Math.max(1000, Number(options.leaseMs || DEFAULT_LEASE_MS));
    this.token = crypto.randomUUID();
    this.held = false;
    this.inode = null;
  }

  _meta() {
    try {
      return JSON.parse(fs.readFileSync(path.join(this.dir, 'owner.json'), 'utf8'));
    } catch (_) {
      return {};
    }
  }

  _isFresh(meta) {
    if (!meta || !meta.token) return false;
    if (!processAlive(meta.pid)) return false;
    const heartbeat = Number(meta.heartbeat || 0);
    return Number.isFinite(heartbeat) && Date.now() - heartbeat <= this.leaseMs;
  }

  _statInode() {
    try {
      return fs.statSync(this.dir).ino;
    } catch (_) {
      return null;
    }
  }

  _ownsPath() {
    return this.inode != null && this._statInode() === this.inode;
  }

  _writeMeta(directory = this.dir) {
    const meta = {
      owner: this.owner,
      token: this.token,
      pid: process.pid,
      heartbeat: Date.now(),
    };
    const temporary = path.join(directory, `.owner-${process.pid}-${Date.now()}.tmp`);
    fs.writeFileSync(temporary, JSON.stringify(meta), { mode: 0o600 });
    fs.renameSync(temporary, path.join(directory, 'owner.json'));
  }

  _publish() {
    const parent = path.dirname(this.dir);
    fs.mkdirSync(parent, { recursive: true, mode: 0o700 });
    const claim = fs.mkdtempSync(path.join(parent, `.${path.basename(this.dir)}.claim-`));
    try {
      fs.chmodSync(claim, 0o700);
      this._writeMeta(claim);
      fs.renameSync(claim, this.dir);
    } catch (_) {
      fs.rmSync(claim, { recursive: true, force: true });
      return false;
    }
    this.inode = this._statInode();
    this.held = true;
    return true;
  }

  tryAcquire() {
    if (this.held) return { ok: true, owner: this.owner };
    for (let attempt = 0; attempt < 3; attempt += 1) {
      if (this._publish()) return { ok: true, owner: this.owner };
      const current = this._meta();
      if (this._isFresh(current)) {
        return { ok: false, owner: String(current.owner || 'unknown'), reason: 'held' };
      }
      const stale = `${this.dir}.stale-${process.pid}-${Date.now()}`;
      try {
        fs.renameSync(this.dir, stale);
        fs.rmSync(stale, { recursive: true, force: true });
      } catch (error) {
        if (error.code !== 'ENOENT') {
          return { ok: false, owner: String(current.owner || 'unknown'), reason: 'stale-race' };
        }
      }
    }
    const winner = this._meta();
    return { ok: false, owner: String(winner.owner || 'unknown'), reason: 'acquire-race' };
  }

  heartbeat() {
    if (!this.held) return false;
    const current = this._meta();
    if (current.token !== this.token || !this._ownsPath()) {
      this.held = false;
      this.inode = null;
      return false;
    }
    this._writeMeta();
    return true;
  }

  release() {
    if (!this.held) return;
    if (this._ownsPath() && this._meta().token === this.token) {
      try { fs.rmSync(path.join(this.dir, 'owner.json'), { force: true }); } catch (_) {}
      try { fs.rmdirSync(this.dir); } catch (_) {}
    }
    this.held = false;
    this.inode = null;
  }

  status() {
    const current = this._meta();
    return {
      owner: this.owner,
      held: this.held,
      currentOwner: String(current.owner || ''),
      leaseFresh: this._isFresh(current),
      lockDir: this.dir,
    };
  }
}

module.exports = {
  SenderAuthorityGuard,
  defaultLockDir,
};
