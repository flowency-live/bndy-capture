package live.bndy.capture

import android.content.Context
import org.json.JSONObject
import java.io.File
import java.net.HttpURLConnection
import java.net.URL

class CaptureApi(
    private val settings: Settings,
    private val context: Context
) {
    private data class UploadedImage(
        val bucket: String,
        val key: String,
        val mimeType: String,
        val size: Long
    )

    fun send(capture: Capture): String? {
        require(settings.apiUrl.isNotBlank()) { "API URL is not configured" }
        require(settings.apiToken.isNotBlank()) { "API token is not configured" }

        val uploadedImage = if (capture.mimeType.startsWith("image/") && !capture.imageUri.isNullOrBlank()) {
            uploadImage(capture)
        } else null

        val connection = URL("${settings.apiUrl}/v1/captures").openConnection() as HttpURLConnection
        connection.requestMethod = "POST"
        connection.connectTimeout = 10_000
        connection.readTimeout = 20_000
        connection.doOutput = true
        connection.setRequestProperty("Content-Type", "application/json")
        connection.setRequestProperty("Authorization", "Bearer ${settings.apiToken}")

        val payload = JSONObject().apply {
            put("capturedAt", capture.capturedAt)
            put("sharedText", capture.sharedText)
            put("sharedUrl", capture.sharedUrl)
            put("mimeType", capture.mimeType)
            put("sourceApp", capture.sourceApp)
            put("note", capture.note)
            put("suggestedEntityType", "unknown")
            if (uploadedImage != null) {
                put("media", JSONObject().apply {
                    put("type", "image")
                    put("bucket", uploadedImage.bucket)
                    put("key", uploadedImage.key)
                    put("mimeType", uploadedImage.mimeType)
                    put("size", uploadedImage.size)
                })
            }
            put("rawPayload", JSONObject().apply {
                put("androidLocalId", capture.localId)
                put("hadSharedImage", capture.imageUri != null)
            })
        }

        connection.outputStream.use { it.write(payload.toString().toByteArray(Charsets.UTF_8)) }
        val code = connection.responseCode
        val body = (if (code in 200..299) connection.inputStream else connection.errorStream)
            ?.bufferedReader()?.use { it.readText() }.orEmpty()
        if (code !in 200..299) throw IllegalStateException("HTTP $code: ${body.take(500)}")
        return if (body.isBlank()) null else JSONObject(body).optString("id").ifBlank { null }
    }

    private fun uploadImage(capture: Capture): UploadedImage {
        val imagePath = capture.imageUri ?: error("Image path missing")
        val file = File(imagePath)
        require(file.exists() && file.isFile) { "Shared image is no longer available" }

        val init = URL("${settings.apiUrl}/v1/uploads/image").openConnection() as HttpURLConnection
        init.requestMethod = "POST"
        init.connectTimeout = 10_000
        init.readTimeout = 15_000
        init.doOutput = true
        init.setRequestProperty("Content-Type", "application/json")
        init.setRequestProperty("Authorization", "Bearer ${settings.apiToken}")
        val initBody = JSONObject().apply { put("mimeType", capture.mimeType) }.toString()
        init.outputStream.use { it.write(initBody.toByteArray(Charsets.UTF_8)) }
        val initCode = init.responseCode
        val initResponse = (if (initCode in 200..299) init.inputStream else init.errorStream)
            ?.bufferedReader()?.use { it.readText() }.orEmpty()
        if (initCode !in 200..299) throw IllegalStateException("Image upload init HTTP $initCode: ${initResponse.take(500)}")
        val details = JSONObject(initResponse)
        val uploadUrl = details.getString("uploadUrl")

        val put = URL(uploadUrl).openConnection() as HttpURLConnection
        put.requestMethod = "PUT"
        put.connectTimeout = 15_000
        put.readTimeout = 30_000
        put.doOutput = true
        put.setRequestProperty("Content-Type", capture.mimeType)
        put.setFixedLengthStreamingMode(file.length())
        file.inputStream().use { input ->
            put.outputStream.use { output -> input.copyTo(output) }
        }
        val putCode = put.responseCode
        if (putCode !in 200..299) {
            val errorBody = put.errorStream?.bufferedReader()?.use { it.readText() }.orEmpty()
            throw IllegalStateException("Image upload HTTP $putCode: ${errorBody.take(500)}")
        }

        return UploadedImage(
            bucket = details.getString("bucket"),
            key = details.getString("key"),
            mimeType = details.getString("mimeType"),
            size = file.length()
        )
    }
}
