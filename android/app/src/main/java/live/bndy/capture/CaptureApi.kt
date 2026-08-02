package live.bndy.capture

import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

class CaptureApi(private val settings: Settings) {
    fun send(capture: Capture): String? {
        require(settings.apiUrl.isNotBlank()) { "API URL is not configured" }
        require(settings.apiToken.isNotBlank()) { "API token is not configured" }

        val connection = URL("${settings.apiUrl}/v1/captures").openConnection() as HttpURLConnection
        connection.requestMethod = "POST"
        connection.connectTimeout = 10_000
        connection.readTimeout = 15_000
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
            put("rawPayload", JSONObject().apply {
                put("androidLocalId", capture.localId)
                put("imageUri", capture.imageUri)
            })
        }

        connection.outputStream.use { it.write(payload.toString().toByteArray(Charsets.UTF_8)) }
        val code = connection.responseCode
        val body = (if (code in 200..299) connection.inputStream else connection.errorStream)
            ?.bufferedReader()?.use { it.readText() }.orEmpty()
        if (code !in 200..299) throw IllegalStateException("HTTP $code: ${body.take(500)}")
        return if (body.isBlank()) null else JSONObject(body).optString("id").ifBlank { null }
    }
}
