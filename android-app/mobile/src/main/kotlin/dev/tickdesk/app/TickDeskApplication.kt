package dev.tickdesk.app

import android.app.Application
import android.content.Context
import androidx.work.*
import dev.tickdesk.app.data.*
import dev.tickdesk.core.*
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import java.net.URI
import java.util.concurrent.TimeUnit
import kotlin.random.Random

class TickDeskApplication : Application() {
    internal val runtime by lazy { WorkbenchRuntime(this) }
}

internal data class PairingDisplay(val pairId: String, val endpoint: String, val pinSummary: String)

private fun PairingConfig.toDisplay(): PairingDisplay {
    val uri = URI(relayUrl)
    val digest = pins.first().removePrefix("sha256/")
    val morePins = if (pins.size > 1) " (+${pins.size - 1})" else ""
    return PairingDisplay(pairId, "${uri.scheme}://${uri.rawAuthority}",
        "${digest.take(8)}…${digest.takeLast(4)}$morePins")
}

internal class WorkbenchRuntime(private val context: Context) {
    val repository = WorkbenchRepository(RoomWorkbenchStore.open(context))
    val state = repository.state
    private val vault = PairingVault(context)
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    val reminders = ReminderPresenter(context, repository, scope) { foreground }
    val reportReminders = ReportReminderPresenter(context, repository)
    private val relay = RelayClient(repository, onMessage = reminders::receive)
    private val mutex = Mutex()
    private val ready = CompletableDeferred<Unit>()
    private var credentials: PairingConfig? = null
    private val mutablePairingDisplay = MutableStateFlow<PairingDisplay?>(null)
    val pairingDisplay = mutablePairingDisplay.asStateFlow()
    private var connection: Job? = null
    @Volatile private var blocked = false
    @Volatile private var foreground = false
    private val visible = MutableStateFlow(false)
    private var backgroundLeases = 0
    val backgroundReminders = BackgroundReminderController(context, this)
    val isVisible: Boolean get() = foreground

    init {
        scope.launch {
            try {
                credentials = vault.read()
                repository.selectPair(credentials?.pairId)
                mutablePairingDisplay.value = credentials?.toDisplay()
            } catch (_: Exception) {
                repository.disconnect("PAIRING_KEY_UNAVAILABLE")
            } finally {
                ready.complete(Unit)
            }
        }
        scope.launch {
            var previous: AuxiliaryRefresh? = null
            combine(state, visible, ::auxiliaryRefresh).distinctUntilChanged().collect { refresh ->
                val last = previous
                previous = refresh
                if (refresh == null) return@collect
                val opened = last?.pair != refresh.pair || last?.epoch != refresh.epoch
                try {
                    if (opened) repository.requestAuxiliary("get_status", refresh.pair)
                    if (opened || last?.files != refresh.files) repository.requestAuxiliary("list_files", refresh.pair)
                    if (opened || last?.reply != refresh.reply) repository.requestAuxiliary("get_messages", refresh.pair)
                } catch (cancel: CancellationException) {
                    throw cancel
                } catch (_: Exception) {
                    // These are read refreshes. Manual requests and the next foreground entry remain available.
                }
            }
        }
        scope.launch {
            var previous = emptyList<ReminderReference>()
            state.collect { snapshot ->
                val current = BackgroundReminderPolicy.currentReferences(snapshot, System.currentTimeMillis())
                val newReferences = current.filterNot { it in previous }
                previous = current
                val pair = snapshot.pairId ?: return@collect
                newReferences.forEach { reference -> reminders.receive(reference, pair) }
            }
        }
        scope.launch {
            state.map { snapshot ->
                val pair = snapshot.pairId
                val epoch = snapshot.epoch
                if (pair != null && epoch != null && snapshot.connection.canWrite) pair to epoch else null
            }.distinctUntilChanged().collect { session ->
                    if (session != null) {
                        try {
                            repository.requestAuxiliary("list_reports", session.first)
                        } catch (cancel: CancellationException) {
                            throw cancel
                        } catch (_: Exception) {
                            // The next confirmed connection retries the optional subscription.
                        }
                    }
                }
        }
        scope.launch {
            var previousPair: String? = null
            var previousEpoch: String? = null
            var previous = emptyList<ReportNotice>()
            state.collect { snapshot ->
                val pair = snapshot.pairId
                val epoch = snapshot.epoch
                val sameSession = pair == previousPair && epoch == previousEpoch
                val notices = if (sameSession) snapshot.reportNotices - previous.toSet() else snapshot.reportNotices
                previousPair = pair
                previousEpoch = epoch
                previous = snapshot.reportNotices
                if (pair != null && epoch != null) {
                    notices.forEach { notice -> launch { reportReminders.present(pair, epoch, notice) } }
                }
            }
        }
    }

    suspend fun pair(config: PairingConfig) = withContext(Dispatchers.IO) {
        ready.await()
        mutex.withLock {
            if (credentials?.pairId != config.pairId) backgroundReminders.setEnabled(false)
            vault.write(config)
            connection?.cancelAndJoin()
            credentials = config
            blocked = false
            repository.selectPair(config.pairId)
            mutablePairingDisplay.value = config.toDisplay()
            repository.disconnect()
            startLocked()
        }
    }

    fun foreground(value: Boolean) {
        foreground = value
        visible.value = value
        if (!value) reminders.dismiss()
        scope.launch {
            ready.await()
            if (foreground) backgroundReminders.resumeVisible()
            mutex.withLock {
                if (foreground) {
                    if (connection?.isActive == true) repository.refresh()
                    startLocked()
                }
                else if (backgroundLeases == 0) stopLocked()
            }
            if (!foreground && needsDelivery()) scheduleRetry()
        }
    }

    fun retry() {
        scope.launch {
            ready.await()
            mutex.withLock {
                blocked = false
                if (connection?.isActive != true) startLocked()
                else repository.refresh()
            }
        }
    }

    fun resumeBackgroundReminders() { scope.launch { ready.await(); backgroundReminders.resumeVisible() } }

    suspend fun enqueue(
        command: String, params: JsonObject, expectedPair: String? = null, expectedEpoch: String? = null,
        expectedPeer: String? = null,
    ): String = withContext(Dispatchers.IO) {
        val id = repository.enqueue(command, params, expectedPair, expectedEpoch, expectedPeer)
        if (needsDelivery()) scheduleRetry()
        id
    }

    suspend fun startUpload(name: String, mime: String, bytes: ByteArray, expectedPairId: String, expectedEpoch: String): String =
        withContext(Dispatchers.IO) {
            repository.startUpload(name, mime, bytes, expectedPairId, expectedEpoch).also { scheduleRetry() }
        }

    suspend fun startHoldingImport(name: String, mime: String, bytes: ByteArray, noticeVersion: String,
                                   expectedPairId: String, expectedEpoch: String): String = withContext(Dispatchers.IO) {
        repository.startHoldingImport(name, mime, bytes, noticeVersion, expectedPairId, expectedEpoch).also { scheduleRetry() }
    }

    suspend fun retryHoldingImport(noticeVersion: String, expectedPairId: String, expectedEpoch: String): String = withContext(Dispatchers.IO) {
        repository.retryHoldingImport(noticeVersion, expectedPairId, expectedEpoch).also { scheduleRetry() }
    }

    suspend fun startDownload(fileId: String, expectedPairId: String, expectedEpoch: String): String =
        withContext(Dispatchers.IO) {
            repository.startDownload(fileId, expectedPairId, expectedEpoch).also { scheduleRetry() }
        }

    suspend fun cancelTransfer(id: String, expectedPairId: String, expectedEpoch: String) = withContext(Dispatchers.IO) {
        repository.cancelTransfer(id, expectedPairId, expectedEpoch)
        if (needsDelivery()) scheduleRetry()
    }

    suspend fun recoveryRequest(command: String, params: JsonObject, expectedEvidence: Set<RecoveryEvidenceSource>?): JsonObject =
        withContext(Dispatchers.IO) {
            val pair = state.value.pairId ?: throw ProtocolException("工作台未配对")
            val id = repository.enqueueRecovery(command, params, pair, expectedEvidence)
            if (needsDelivery()) scheduleRetry()
            val projection = withTimeoutOrNull(20_000) {
                state.first { current ->
                    current.pairId != pair || current.intents.any { it.id == id && it.stage !in transportStages }
                }
            } ?: throw ProtocolException("业务回执尚未到达，原操作已保留")
            if (projection.pairId != pair) throw ProtocolException("工作台已切换")
            val intent = projection.intents.firstOrNull { it.id == id }
            intent?.result ?: throw ProtocolException(intent?.errorCode ?: "恢复操作尚未得到确认")
        }

    suspend fun pairedScope(): String? {
        ready.await()
        return state.value.pairId
    }

    suspend fun registerPush(regId: String): String? = withContext(Dispatchers.IO) {
        val pair = pairedScope() ?: return@withContext null
        val snapshot = state.value
        if (!snapshot.connection.canWrite || snapshot.meta?.obj("settings")?.flag("pushEnabled") != true) return@withContext null
        val deviceId = Protocol.frame(repository.hello()).json.text("incarnation") ?: return@withContext null
        val existing = snapshot.intents.lastOrNull {
            it.command == "register_push" && it.data.text("regId") == regId &&
                it.data.text("serverEpoch") == snapshot.epoch && it.stage !in registrationTerminal
        }
        if (existing != null) return@withContext existing.id
        repository.enqueue("register_push", buildJsonObject {
            put("platform", "android"); put("regId", regId); put("deviceId", deviceId)
        }, expectedPair = pair).also { scheduleRetry() }
    }

    suspend fun backgroundWork(): Boolean = withBackgroundLink {
        if (!needsDelivery()) return@withBackgroundLink true
        withTimeoutOrNull(40_000) { state.first { !needsDelivery(it) || blocked } }
        !needsDelivery() || blocked
    }

    /** The foreground service owns this lease; cancellation releases it without disturbing visible UI. */
    suspend fun withReminderConnection(block: suspend () -> Unit) = withBackgroundLink(block)

    suspend fun backgroundReminder(reference: ReminderReference, pair: String): ReminderOutcome {
        if (!reference.inWindow(System.currentTimeMillis()) || pairedScope() != pair || blocked) return ReminderOutcome.DONE
        return withBackgroundLink { reminders.resolve(reference, pair, fromPush = true) }
    }

    private suspend fun <T> withBackgroundLink(block: suspend () -> T): T {
        ready.await()
        mutex.withLock {
            backgroundLeases++
            startLocked()
        }
        try { return block() }
        finally {
            withContext(NonCancellable) {
                mutex.withLock {
                    backgroundLeases--
                    if (!foreground && backgroundLeases == 0) {
                        relay.flushWrites()
                        stopLocked()
                    }
                }
            }
        }
    }

    private fun startLocked() {
        val config = credentials ?: return
        if (blocked || connection?.isActive == true || (!foreground && backgroundLeases == 0)) return
        connection = scope.launch {
            var failures = 0
            while (isActive) {
                val ended = relay.run(config)
                if (!ended.retry) blocked = true
                repository.disconnect(ended.reason)
                if (!ended.retry) return@launch
                if (ended.healthy) failures = 0
                val ceiling = minOf(30_000L, 1_000L shl failures.coerceAtMost(5))
                failures++
                delay(maxOf(ended.delayMs, Random.nextLong(ceiling + 1)))
            }
        }
    }

    private suspend fun stopLocked() {
        connection?.cancelAndJoin()
        connection = null
        if (!blocked) repository.disconnect()
    }

    private fun needsDelivery(projection: WorkbenchProjection = state.value) =
        projection.hasPendingReceipts || projection.hasPendingTransfers || projection.hasPendingHoldingImport ||
            projection.intents.any { it.stage in retryStages }

    private fun scheduleRetry() {
        val request = OneTimeWorkRequestBuilder<DeliveryRetryWorker>()
            .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
            .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 15, TimeUnit.SECONDS)
            .build()
        WorkManager.getInstance(context).enqueueUniqueWork("tickdesk-delivery-v2", ExistingWorkPolicy.KEEP, request)
    }

    private companion object {
        val transportStages = setOf(DeliveryStage.PENDING, DeliveryStage.SENT, DeliveryStage.DELIVERED)
        val retryStages = transportStages + DeliveryStage.WAITING_FOR_SNAPSHOT
        val registrationTerminal = setOf(DeliveryStage.REJECTED, DeliveryStage.CANCELLED, DeliveryStage.QUARANTINED)
    }
}

private data class AuxiliaryRefresh(val pair: String, val epoch: String, val files: Pair<String?, String?>, val reply: String?)

private fun auxiliaryRefresh(state: WorkbenchProjection, visible: Boolean): AuxiliaryRefresh? {
    if (!visible || !state.connection.canWrite) return null
    val pair = state.pairId ?: return null
    val epoch = state.epoch ?: return null
    val saved = state.transfers.lastOrNull { it.kind == FileTransferKind.UPLOAD && it.status == FileTransferStatus.COMPLETE }
    val deleted = state.intents.lastOrNull {
        it.command == "delete_file" && it.data.text("serverEpoch") == epoch && it.result != null &&
            it.stage in setOf(DeliveryStage.WAITING_FOR_SNAPSHOT, DeliveryStage.APPLIED)
    }
    return AuxiliaryRefresh(pair, epoch, saved?.id to deleted?.id, state.replies.lastOrNull()?.text("requestId"))
}

class DeliveryRetryWorker(context: Context, parameters: WorkerParameters) : CoroutineWorker(context, parameters) {
    override suspend fun doWork(): Result {
        val runtime = (applicationContext as TickDeskApplication).runtime
        return if (runtime.backgroundWork()) Result.success() else Result.retry()
    }
}
