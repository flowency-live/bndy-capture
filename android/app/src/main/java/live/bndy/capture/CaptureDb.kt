package live.bndy.capture

import android.content.ContentValues
import android.content.Context
import android.database.Cursor
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper

class CaptureDb(context: Context) : SQLiteOpenHelper(context, "bndy-captures.db", null, 1) {
    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL("""
            CREATE TABLE captures (
                local_id INTEGER PRIMARY KEY AUTOINCREMENT,
                captured_at TEXT NOT NULL,
                shared_text TEXT,
                shared_url TEXT,
                mime_type TEXT NOT NULL,
                source_app TEXT,
                image_uri TEXT,
                note TEXT,
                delivery_status TEXT NOT NULL,
                remote_id TEXT,
                last_error TEXT
            )
        """.trimIndent())
    }

    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) = Unit

    fun insert(capturedAt: String, sharedText: String?, sharedUrl: String?, mimeType: String, sourceApp: String?, imageUri: String?, note: String?): Long {
        val values = ContentValues().apply {
            put("captured_at", capturedAt)
            put("shared_text", sharedText)
            put("shared_url", sharedUrl)
            put("mime_type", mimeType)
            put("source_app", sourceApp)
            put("image_uri", imageUri)
            put("note", note)
            put("delivery_status", "pending")
        }
        return writableDatabase.insertOrThrow("captures", null, values)
    }

    fun list(limit: Int = 100): List<Capture> {
        val result = mutableListOf<Capture>()
        readableDatabase.query("captures", null, null, null, null, null, "local_id DESC", limit.coerceIn(1, 500).toString()).use { cursor ->
            while (cursor.moveToNext()) {
                result += Capture(
                    localId = cursor.getLong(cursor.getColumnIndexOrThrow("local_id")),
                    capturedAt = cursor.getString(cursor.getColumnIndexOrThrow("captured_at")),
                    sharedText = cursor.getNullable("shared_text"),
                    sharedUrl = cursor.getNullable("shared_url"),
                    mimeType = cursor.getString(cursor.getColumnIndexOrThrow("mime_type")),
                    sourceApp = cursor.getNullable("source_app"),
                    imageUri = cursor.getNullable("image_uri"),
                    note = cursor.getNullable("note"),
                    deliveryStatus = cursor.getString(cursor.getColumnIndexOrThrow("delivery_status")),
                    remoteId = cursor.getNullable("remote_id"),
                    lastError = cursor.getNullable("last_error")
                )
            }
        }
        return result
    }

    fun get(localId: Long): Capture? = list(500).firstOrNull { it.localId == localId }
    fun pending(): List<Capture> = list(500).filter { it.deliveryStatus != "sent" }

    fun markSent(localId: Long, remoteId: String?) {
        writableDatabase.update("captures", ContentValues().apply {
            put("delivery_status", "sent")
            put("remote_id", remoteId)
            putNull("last_error")
        }, "local_id = ?", arrayOf(localId.toString()))
    }

    fun markFailed(localId: Long, error: String) {
        writableDatabase.update("captures", ContentValues().apply {
            put("delivery_status", "failed")
            put("last_error", error.take(1000))
        }, "local_id = ?", arrayOf(localId.toString()))
    }

    private fun Cursor.getNullable(column: String): String? {
        val index = getColumnIndexOrThrow(column)
        return if (isNull(index)) null else getString(index)
    }
}
