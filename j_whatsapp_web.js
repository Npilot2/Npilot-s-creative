#!/usr/bin/env node
'use strict'
/**
 * J в WhatsApp Web (без токена Meta API — подключение по QR-коду как «Связанное устройство»).
 * Передаёт сообщения и медиафайлы в единое ядро j_core.py на http://127.0.0.1:J_PORT/chat.
 */

const fs = require('fs')
const http = require('http')
const https = require('https')
const path = require('path')

const BASE_DIR = __dirname

function loadEnv(envPath) {
  try {
    const raw = fs.readFileSync(envPath, 'utf8')
    for (const line of raw.split(/\r?\n/)) {
      const s = line.trim()
      if (!s || s.startsWith('#') || !s.includes('=')) continue
      const idx = s.indexOf('=')
      const k = s.slice(0, idx).trim()
      const v = s.slice(idx + 1).trim().replace(/^["']|["']$/g, '')
      if (!(k in process.env)) process.env[k] = v
    }
  } catch (_) {}
}

loadEnv(process.env.J_ENV || path.join(BASE_DIR, 'j.env'))

let Client, LocalAuth, qrcode
try {
  ({ Client, LocalAuth } = require('whatsapp-web.js'))
  qrcode = require('qrcode-terminal')
} catch (err) {
  console.error('[whatsapp-web] Не установлены зависимости Node.js (whatsapp-web.js, qrcode-terminal).')
  console.error('Установи их в папке проекта D:\\SDJ:')
  console.error('  npm install whatsapp-web.js qrcode-terminal')
  process.exit(1)
}

const BRIDGE = (process.env.J_CORE_URL || ('http://127.0.0.1:' + (process.env.J_PORT || '8765'))).replace(/\/+$/, '')
const PREFIX = (process.env.WHATSAPP_PREFIX || '').trim()
const AUTH_DIR = (process.env.WHATSAPP_DATA_DIR || '').trim() || path.join(BASE_DIR, '.wwebjs_auth')
const ALLOWED = new Set(
  (process.env.J_WHATSAPP_ALLOWED || '')
    .split(',')
    .map((x) => x.replace(/\D/g, ''))
    .filter(Boolean)
)

const MAX_CHUNK = 4000
const MEDIA_MAX_BYTES = 8 * 1024 * 1024

function chunks(text, size = MAX_CHUNK) {
  const out = []
  for (let i = 0; i < text.length; i += size) out.push(text.slice(i, i + size))
  return out
}

function classifyMime(mime, filename) {
  const m = String(mime || '').toLowerCase()
  const ext = path.extname(filename || '').toLowerCase()
  if (m.startsWith('image/') || ['.png', '.jpg', '.jpeg', '.gif', '.webp'].includes(ext)) return 'image'
  if (m.startsWith('video/') || ['.mp4', '.mov', '.webm', '.mkv'].includes(ext)) return 'video'
  if (m.startsWith('audio/') || ['.mp3', '.ogg', '.wav', '.m4a', '.opus'].includes(ext)) return 'audio'
  if (m.startsWith('text/') || ['.txt', '.md', '.py', '.js', '.json', '.csv', '.log'].includes(ext)) return 'text'
  if (m === 'application/pdf' || ['.pdf', '.doc', '.docx', '.xls', '.xlsx'].includes(ext)) return 'document'
  return 'other'
}

function postChat(payload) {
  return new Promise((resolve, reject) => {
    const u = new URL(BRIDGE + '/chat')
    const data = JSON.stringify(payload)
    const lib = u.protocol === 'https:' ? https : http
    const req = lib.request(
      u,
      {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Content-Length': Buffer.byteLength(data, 'utf8'),
        },
        timeout: 60000,
      },
      (res) => {
        const parts = []
        res.on('data', (c) => parts.push(c))
        res.on('end', () => {
          try {
            const obj = JSON.parse(Buffer.concat(parts).toString('utf8') || '{}')
            resolve(obj.reply || '')
          } catch (e) {
            reject(e)
          }
        })
      }
    )
    req.on('error', reject)
    req.on('timeout', () => req.destroy(new Error('timeout')))
    req.write(data)
    req.end()
  })
}

const client = new Client({
  authStrategy: new LocalAuth({ dataPath: AUTH_DIR }),
  puppeteer: {
    headless: true,
    args: ['--no-sandbox', '--disable-setuid-sandbox'],
  },
})

client.on('qr', (qr) => {
  console.log('\n[whatsapp-web] Отсканируй этот QR-код в WhatsApp на телефоне:')
  console.log('  Настройки → Связанные устройства → Привязать устройство\n')
  qrcode.generate(qr, { small: true })
})

client.on('authenticated', () => {
  console.log('[whatsapp-web] Сессия авторизована и сохранена локально.')
})

client.on('auth_failure', (msg) => {
  console.error('[whatsapp-web] Сбой авторизации:', msg)
})

client.on('ready', () => {
  console.log('[whatsapp-web] J в WhatsApp Web на связи! (без токена Meta API)')
})

client.on('message', async (msg) => {
  try {
    if (!msg || msg.fromMe || msg.from === 'status@broadcast') return
    const rawFrom = String(msg.from || '')
    const waId = rawFrom.replace(/@.*/, '').replace(/\D/g, '') || rawFrom.replace(/@.*/, '')
    if (ALLOWED.size > 0 && !ALLOWED.has(waId)) return

    let text = String(msg.body || '').trim()
    if (PREFIX) {
      if (!text.toLowerCase().startsWith(PREFIX.toLowerCase())) return
      text = text.slice(PREFIX.length).trim()
    }

    const media = []
    if (msg.hasMedia) {
      try {
        const dl = await msg.downloadMedia()
        if (dl && dl.data) {
          const mime = dl.mimetype || 'application/octet-stream'
          const ext = mime.includes('/') ? mime.split('/')[1].split(';')[0] : 'bin'
          const filename = dl.filename || `media.${ext}`
          const kind = classifyMime(mime, filename)
          const rawBytes = Buffer.from(dl.data, 'base64')
          const item = { filename, mime, kind, size: rawBytes.length }
          if (kind === 'text' && rawBytes.length <= 256 * 1024) {
            item.text_preview = rawBytes.toString('utf8').slice(0, 1500)
          } else if ((['image', 'video', 'audio'].includes(kind) || mime === 'application/pdf') && rawBytes.length <= MEDIA_MAX_BYTES) {
            item.data_b64 = dl.data
          }
          media.push(item)
        }
      } catch (err) {
        console.log('[whatsapp-web] ошибка загрузки медиа:', err.message)
      }
    }

    if (!text && media.length === 0) return

    let senderName = waId
    try {
      const contact = await msg.getContact()
      senderName = contact.pushname || contact.name || waId
    } catch (_) {}

    let reply = ''
    try {
      reply = await postChat({
        user: senderName,
        id: waId,
        text,
        game: 'whatsapp',
        media: media.length > 0 ? media : undefined,
      })
    } catch (err) {
      reply = `Ядро J не отвечает. Запусти: python j_launcher.py start core (${err.message})`
    }

    if (reply) {
      for (const part of chunks(reply)) {
        await msg.reply(part)
      }
    }
  } catch (err) {
    console.error('[whatsapp-web] ошибка сообщения:', err)
  }
})

client.initialize()
