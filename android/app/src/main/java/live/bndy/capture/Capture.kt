package live.bndy.capture

data class Capture(
    val localId: Long,
    val capturedAt: String,
    val sharedText: String?,
    val sharedUrl: String?,
    val mimeType: String,
    val sourceApp: String?,
    val imageUri: String?,
    val note: String?,
    val deliveryStatus: String,
    val remoteId: String?,
    val lastError: String?
)
