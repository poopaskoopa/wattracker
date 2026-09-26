package com.wattracker.android.screens

import android.os.Build
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.OutlinedTextFieldDefaults
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import com.wattracker.android.R
import com.wattracker.android.WatTrackerApp
import com.wattracker.android.cloud.CloudDevice
import com.wattracker.android.cloud.CloudKind
import com.wattracker.android.cloud.CloudPayload
import com.wattracker.android.cloud.CloudRoute
import com.wattracker.android.cloud.DataSource
import com.wattracker.android.cloud.DeviceKeyStore
import com.wattracker.android.cloud.LocalCredentials
import com.wattracker.android.cloud.LocalPairingFailureMessage
import com.wattracker.android.cloud.PairingCode
import com.wattracker.android.cloud.PairingFailureMessage
import com.wattracker.android.cloud.RemoveDeviceFailureMessage
import com.wattracker.android.cloud.RemoveDeviceResult
import com.wattracker.android.shell.Panel
import com.wattracker.android.shell.ScrollableScreenScaffold
import com.wattracker.android.ui.theme.Palette
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import kotlinx.coroutines.launch

/**
 * Settings (#195): backend pairing (cloud / local), device state, key type.
 */
@Composable
fun SettingsScreen() {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val activeSource by WatTrackerApp.dataSourceFlow.collectAsState()

    var viewSource by remember { mutableStateOf(activeSource) }
    var isLoading by remember { mutableStateOf(true) }
    var isCloudPaired by remember { mutableStateOf(false) }
    var isLocalPaired by remember { mutableStateOf(false) }
    var cloudProfileName by remember { mutableStateOf<String?>(null) }
    var currentCredentialId by remember { mutableStateOf<String?>(null) }
    var localCreds by remember { mutableStateOf<LocalCredentials?>(null) }
    var registeredDevices by remember { mutableStateOf<List<CloudDevice>>(emptyList()) }
    var globalNotice by remember { mutableStateOf<String?>(null) }

    val refreshState = {
        scope.launch {
            isLoading = true
            try {
                val s = WatTrackerApp.session(context)
                val lc = WatTrackerApp.localClient(context)
                isCloudPaired = s.isPaired
                isLocalPaired = lc.isPaired
                localCreds = lc.credentials

                if (s.isPaired) {
                    val cachedProfile = s.cached(CloudRoute.Profile)
                    val profileItem = cachedProfile?.items?.firstOrNull { it.kind == CloudKind.Profile }
                    val profile = (profileItem?.payload as? CloudPayload.Profile)?.value
                    cloudProfileName = profile?.displayName
                    currentCredentialId = WatTrackerApp.currentCredentialId
                    registeredDevices = WatTrackerApp.listCloudDevices(context)
                } else {
                    cloudProfileName = null
                    registeredDevices = emptyList()
                }
            } catch (_: Exception) {
                // Keep standing UI state on refresh error
            } finally {
                isLoading = false
            }
        }
    }

    LaunchedEffect(Unit) {
        refreshState()
    }

    ScrollableScreenScaffold(
        title = stringResource(R.string.destination_settings),
        subtitle = stringResource(R.string.screen_settings_subtitle),
    ) {
        if (globalNotice != null) {
            Box(
                modifier = Modifier
                    .fillMaxWidth()
                    .background(Palette.accent.copy(alpha = 0.15f), RoundedCornerShape(8.dp))
                    .border(1.dp, Palette.accent, RoundedCornerShape(8.dp))
                    .padding(12.dp),
            ) {
                Text(
                    text = globalNotice!!,
                    style = MaterialTheme.typography.bodySmall,
                    color = Palette.accent,
                )
            }
        }

        if (isLoading) {
            Box(modifier = Modifier.fillMaxWidth().padding(16.dp), contentAlignment = Alignment.Center) {
                CircularProgressIndicator(color = Palette.accent)
            }
        } else {
            BackendSelector(
                viewSource = viewSource,
                activeSource = activeSource,
                onViewSourceSelected = { viewSource = it },
                onSetActiveSource = { source ->
                    WatTrackerApp.setDataSource(context, source)
                    refreshState()
                },
            )

            Spacer(modifier = Modifier.height(4.dp))

            if (viewSource == DataSource.Cloud) {
                CloudBackendPanel(
                    isPaired = isCloudPaired,
                    isActive = activeSource == DataSource.Cloud,
                    profileName = cloudProfileName,
                    registeredDevices = registeredDevices,
                    currentCredentialId = currentCredentialId,
                    onPairSuccess = {
                        globalNotice = null
                        WatTrackerApp.setDataSource(context, DataSource.Cloud)
                        refreshState()
                    },
                    onRemoveResult = { notice ->
                        globalNotice = notice
                        refreshState()
                    },
                )
            } else {
                LocalBackendPanel(
                    isPaired = isLocalPaired,
                    isActive = activeSource == DataSource.Local,
                    credentials = localCreds,
                    onPairSuccess = {
                        globalNotice = null
                        WatTrackerApp.setDataSource(context, DataSource.Local)
                        refreshState()
                    },
                    onRemoveSuccess = {
                        globalNotice = null
                        refreshState()
                    },
                )
            }
        }
    }
}

@Composable
private fun BackendSelector(
    viewSource: DataSource,
    activeSource: DataSource,
    onViewSourceSelected: (DataSource) -> Unit,
    onSetActiveSource: (DataSource) -> Unit,
) {
    Panel {
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(
                text = stringResource(R.string.settings_backend_title),
                style = MaterialTheme.typography.titleMedium,
                color = Palette.textBright,
                fontWeight = FontWeight.SemiBold,
            )
            if (viewSource == activeSource) {
                Box(
                    modifier = Modifier
                        .background(Palette.accent.copy(alpha = 0.2f), RoundedCornerShape(4.dp))
                        .padding(horizontal = 8.dp, vertical = 4.dp),
                ) {
                    Text(
                        text = stringResource(R.string.settings_active_badge),
                        style = MaterialTheme.typography.labelSmall,
                        color = Palette.accent,
                        fontWeight = FontWeight.Bold,
                    )
                }
            } else {
                OutlinedButton(
                    onClick = { onSetActiveSource(viewSource) },
                    colors = ButtonDefaults.outlinedButtonColors(contentColor = Palette.accent),
                    modifier = Modifier.height(32.dp),
                ) {
                    Text(stringResource(R.string.settings_set_active), style = MaterialTheme.typography.labelSmall)
                }
            }
        }
        Spacer(modifier = Modifier.height(8.dp))
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .background(Palette.surfaceInset, RoundedCornerShape(8.dp))
                .padding(4.dp),
        ) {
            SelectorTab(
                title = stringResource(R.string.settings_tab_cloud),
                selected = viewSource == DataSource.Cloud,
                modifier = Modifier.weight(1f),
                onClick = { onViewSourceSelected(DataSource.Cloud) },
            )
            SelectorTab(
                title = stringResource(R.string.settings_tab_local),
                selected = viewSource == DataSource.Local,
                modifier = Modifier.weight(1f),
                onClick = { onViewSourceSelected(DataSource.Local) },
            )
        }
    }
}

@Composable
private fun SelectorTab(
    title: String,
    selected: Boolean,
    modifier: Modifier = Modifier,
    onClick: () -> Unit,
) {
    Box(
        modifier = modifier
            .clip(RoundedCornerShape(6.dp))
            .background(if (selected) Palette.panel else Color.Transparent)
            .clickable(onClick = onClick)
            .padding(vertical = 10.dp),
        contentAlignment = Alignment.Center,
    ) {
        Text(
            text = title,
            style = MaterialTheme.typography.labelMedium,
            fontWeight = if (selected) FontWeight.Bold else FontWeight.Normal,
            color = if (selected) Palette.accent else Palette.muted,
        )
    }
}

@Composable
private fun CloudBackendPanel(
    isPaired: Boolean,
    isActive: Boolean,
    profileName: String?,
    registeredDevices: List<CloudDevice>,
    currentCredentialId: String?,
    onPairSuccess: () -> Unit,
    onRemoveResult: (notice: String?) -> Unit,
) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()

    if (isPaired) {
        Panel {
            Text(
                text = stringResource(R.string.settings_cloud_paired_title),
                style = MaterialTheme.typography.titleMedium,
                color = Palette.ok,
                fontWeight = FontWeight.Bold,
            )
            Spacer(modifier = Modifier.height(8.dp))

            if (profileName != null) {
                Text(
                    text = "Rider: $profileName",
                    style = MaterialTheme.typography.bodyMedium,
                    fontWeight = FontWeight.SemiBold,
                    color = Palette.textBright,
                )
            }

            val keyKindStr = when (WatTrackerApp.keyKind) {
                DeviceKeyStore.KeyKind.StrongBox -> "StrongBox Hardware Key"
                DeviceKeyStore.KeyKind.TEE -> "TEE Hardware Key"
                DeviceKeyStore.KeyKind.Software -> "Software Key (Fallback)"
                null -> "Keystore P-256"
            }

            Text(
                text = "Signing Key: $keyKindStr",
                style = MaterialTheme.typography.bodyMedium,
                color = Palette.text,
            )
            val currentDevice = registeredDevices.firstOrNull { it.credentialId == currentCredentialId }
            if (currentDevice?.createdAt != null) {
                val pairedDateStr = remember(currentDevice.createdAt) {
                    val df = SimpleDateFormat("yyyy-MM-dd HH:mm", Locale.getDefault())
                    df.format(Date((currentDevice.createdAt * 1000).toLong()))
                }
                Text(
                    text = stringResource(R.string.settings_paired_at, pairedDateStr),
                    style = MaterialTheme.typography.bodySmall,
                    color = Palette.muted,
                )
            }
            Text(
                text = "Capabilities: Read-only",
                style = MaterialTheme.typography.bodySmall,
                color = Palette.muted,
            )

            Spacer(modifier = Modifier.height(16.dp))

            var isRemoving by remember { mutableStateOf(false) }
            var removeError by remember { mutableStateOf<String?>(null) }

            if (removeError != null) {
                Text(
                    text = removeError!!,
                    style = MaterialTheme.typography.bodySmall,
                    color = Palette.alert,
                    modifier = Modifier.padding(bottom = 8.dp),
                )
            }

            val fallbackNoticeStr = stringResource(R.string.settings_cloud_remove_fallback_notice)

            Button(
                onClick = {
                    isRemoving = true
                    removeError = null
                    scope.launch {
                        try {
                            when (val result = WatTrackerApp.removeCloudDevice(context)) {
                                is RemoveDeviceResult.ServerRevoked -> {
                                    onRemoveResult(null)
                                }
                                is RemoveDeviceResult.LocalFallback -> {
                                    onRemoveResult(fallbackNoticeStr)
                                }
                                is RemoveDeviceResult.Failed -> {
                                    removeError = result.message
                                }
                            }
                        } catch (e: Exception) {
                            removeError = RemoveDeviceFailureMessage.text(e)
                        } finally {
                            isRemoving = false
                        }
                    }
                },
                enabled = !isRemoving,
                colors = ButtonDefaults.buttonColors(containerColor = Palette.alert, contentColor = Palette.onAccent),
            ) {
                Text(if (isRemoving) stringResource(R.string.settings_removing) else stringResource(R.string.settings_remove_device))
            }

            if (registeredDevices.isNotEmpty()) {
                Spacer(modifier = Modifier.height(16.dp))
                Text(
                    text = "Registered Account Devices",
                    style = MaterialTheme.typography.titleSmall,
                    color = Palette.textBright,
                    fontWeight = FontWeight.SemiBold,
                )
                Spacer(modifier = Modifier.height(6.dp))
                registeredDevices.forEach { dev ->
                    DeviceRow(device = dev, isCurrent = dev.credentialId == currentCredentialId)
                }
            }
        }
    } else {
        Panel {
            Text(
                text = stringResource(R.string.settings_cloud_unpaired_title),
                style = MaterialTheme.typography.titleMedium,
                color = Palette.textBright,
                fontWeight = FontWeight.Bold,
            )
            Text(
                text = stringResource(R.string.settings_cloud_unpaired_sub),
                style = MaterialTheme.typography.bodySmall,
                color = Palette.muted,
            )

            if (WatTrackerApp.credentialStoreDegraded) {
                Spacer(modifier = Modifier.height(8.dp))
                Box(
                    modifier = Modifier
                        .fillMaxWidth()
                        .background(Palette.alert.copy(alpha = 0.15f), RoundedCornerShape(8.dp))
                        .border(1.dp, Palette.alert, RoundedCornerShape(8.dp))
                        .padding(12.dp),
                ) {
                    Text(
                        text = stringResource(R.string.settings_cloud_degraded_warning),
                        style = MaterialTheme.typography.bodySmall,
                        color = Palette.alert,
                    )
                }
            }

            Spacer(modifier = Modifier.height(12.dp))

            var codeInput by remember { mutableStateOf("") }
            var labelInput by remember { mutableStateOf(defaultDeviceLabel()) }
            var isPairing by remember { mutableStateOf(false) }
            var errorMessage by remember { mutableStateOf<String?>(null) }

            OutlinedTextField(
                value = codeInput,
                onValueChange = { input ->
                    val raw = input.uppercase()
                    codeInput = PairingCode.grouped(raw) ?: raw
                },
                label = { Text(stringResource(R.string.settings_code_label)) },
                singleLine = true,
                modifier = Modifier.fillMaxWidth(),
                colors = customTextFieldColors(),
            )

            Spacer(modifier = Modifier.height(8.dp))

            OutlinedTextField(
                value = labelInput,
                onValueChange = { labelInput = it },
                label = { Text(stringResource(R.string.settings_device_label)) },
                singleLine = true,
                modifier = Modifier.fillMaxWidth(),
                colors = customTextFieldColors(),
            )

            if (errorMessage != null) {
                Spacer(modifier = Modifier.height(8.dp))
                Text(
                    text = errorMessage!!,
                    style = MaterialTheme.typography.bodySmall,
                    color = Palette.alert,
                )
            }

            Spacer(modifier = Modifier.height(12.dp))

            Button(
                onClick = {
                    isPairing = true
                    errorMessage = null
                    scope.launch {
                        try {
                            val canonical = PairingCode.normalized(codeInput)
                            if (canonical == null) {
                                errorMessage = "Check the pairing code characters and length."
                                isPairing = false
                                return@launch
                            }
                            WatTrackerApp.pairCloud(context, canonical, labelInput.ifBlank { null })
                            onPairSuccess()
                        } catch (e: Exception) {
                            errorMessage = PairingFailureMessage.text(e)
                        } finally {
                            isPairing = false
                        }
                    }
                },
                enabled = !isPairing && codeInput.isNotBlank(),
                colors = ButtonDefaults.buttonColors(containerColor = Palette.accent, contentColor = Palette.onAccent),
            ) {
                if (isPairing) {
                    CircularProgressIndicator(modifier = Modifier.width(16.dp), color = Palette.onAccent, strokeWidth = 2.dp)
                    Spacer(modifier = Modifier.width(8.dp))
                    Text(stringResource(R.string.settings_pairing))
                } else {
                    Text(stringResource(R.string.settings_pair_button))
                }
            }
        }
    }
}

@Composable
private fun LocalBackendPanel(
    isPaired: Boolean,
    isActive: Boolean,
    credentials: LocalCredentials?,
    onPairSuccess: () -> Unit,
    onRemoveSuccess: () -> Unit,
) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()

    if (isPaired && credentials != null) {
        Panel {
            Text(
                text = stringResource(R.string.settings_local_paired_title),
                style = MaterialTheme.typography.titleMedium,
                color = Palette.ok,
                fontWeight = FontWeight.Bold,
            )
            Spacer(modifier = Modifier.height(8.dp))

            Text(
                text = "Server Address: ${credentials.serverUrl}",
                style = MaterialTheme.typography.bodyMedium,
                color = Palette.text,
            )
            if (credentials.username != null) {
                Text(
                    text = "Rider: ${credentials.username}",
                    style = MaterialTheme.typography.bodySmall,
                    color = Palette.muted,
                )
            }

            val pairedDateStr = remember(credentials.pairedAtMillis) {
                val df = SimpleDateFormat("yyyy-MM-dd HH:mm", Locale.getDefault())
                df.format(Date(credentials.pairedAtMillis))
            }
            Text(
                text = stringResource(R.string.settings_paired_at, pairedDateStr),
                style = MaterialTheme.typography.bodySmall,
                color = Palette.muted,
            )

            Spacer(modifier = Modifier.height(16.dp))

            var isRemoving by remember { mutableStateOf(false) }
            var removeError by remember { mutableStateOf<String?>(null) }

            if (removeError != null) {
                Text(
                    text = removeError!!,
                    style = MaterialTheme.typography.bodySmall,
                    color = Palette.alert,
                    modifier = Modifier.padding(bottom = 8.dp),
                )
            }

            val localRemoveFailedStr = stringResource(R.string.settings_local_remove_failed)

            Button(
                onClick = {
                    isRemoving = true
                    removeError = null
                    scope.launch {
                        try {
                            WatTrackerApp.removeLocalDevice(context)
                            onRemoveSuccess()
                        } catch (_: Exception) {
                            removeError = localRemoveFailedStr
                        } finally {
                            isRemoving = false
                        }
                    }
                },
                enabled = !isRemoving,
                colors = ButtonDefaults.buttonColors(containerColor = Palette.alert, contentColor = Palette.onAccent),
            ) {
                Text(if (isRemoving) stringResource(R.string.settings_removing) else stringResource(R.string.settings_remove_device))
            }

            Spacer(modifier = Modifier.height(8.dp))
            Text(
                text = stringResource(R.string.settings_local_revoke_notice),
                style = MaterialTheme.typography.labelSmall,
                color = Palette.muted,
            )
        }
    } else {
        Panel {
            Text(
                text = stringResource(R.string.settings_local_unpaired_title),
                style = MaterialTheme.typography.titleMedium,
                color = Palette.textBright,
                fontWeight = FontWeight.Bold,
            )
            Text(
                text = stringResource(R.string.settings_local_unpaired_sub),
                style = MaterialTheme.typography.bodySmall,
                color = Palette.muted,
            )

            Spacer(modifier = Modifier.height(12.dp))

            var urlInput by remember { mutableStateOf("") }
            var tokenInput by remember { mutableStateOf("") }
            var isPairing by remember { mutableStateOf(false) }
            var errorMessage by remember { mutableStateOf<String?>(null) }

            OutlinedTextField(
                value = urlInput,
                onValueChange = { urlInput = it },
                label = { Text(stringResource(R.string.settings_server_url_label)) },
                singleLine = true,
                modifier = Modifier.fillMaxWidth(),
                colors = customTextFieldColors(),
            )

            Spacer(modifier = Modifier.height(8.dp))

            OutlinedTextField(
                value = tokenInput,
                onValueChange = { tokenInput = it },
                label = { Text(stringResource(R.string.settings_token_label)) },
                singleLine = true,
                modifier = Modifier.fillMaxWidth(),
                colors = customTextFieldColors(),
                keyboardOptions = KeyboardOptions(
                    keyboardType = KeyboardType.Password,
                    autoCorrectEnabled = false,
                ),
                visualTransformation = PasswordVisualTransformation(),
            )

            if (errorMessage != null) {
                Spacer(modifier = Modifier.height(8.dp))
                Text(
                    text = errorMessage!!,
                    style = MaterialTheme.typography.bodySmall,
                    color = Palette.alert,
                )
            }

            Spacer(modifier = Modifier.height(12.dp))

            Button(
                onClick = {
                    isPairing = true
                    errorMessage = null
                    scope.launch {
                        try {
                            WatTrackerApp.pairLocal(context, urlInput, tokenInput)
                            onPairSuccess()
                        } catch (e: Exception) {
                            errorMessage = LocalPairingFailureMessage.text(e)
                        } finally {
                            isPairing = false
                        }
                    }
                },
                enabled = !isPairing && urlInput.isNotBlank() && tokenInput.isNotBlank(),
                colors = ButtonDefaults.buttonColors(containerColor = Palette.accent, contentColor = Palette.onAccent),
            ) {
                if (isPairing) {
                    CircularProgressIndicator(modifier = Modifier.width(16.dp), color = Palette.onAccent, strokeWidth = 2.dp)
                    Spacer(modifier = Modifier.width(8.dp))
                    Text(stringResource(R.string.settings_connecting))
                } else {
                    Text(stringResource(R.string.settings_pair_local_button))
                }
            }
        }
    }
}

@Composable
private fun DeviceRow(device: CloudDevice, isCurrent: Boolean) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .padding(vertical = 4.dp)
            .background(Palette.surfaceInset, RoundedCornerShape(6.dp))
            .padding(8.dp),
        horizontalArrangement = Arrangement.SpaceBetween,
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Column {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    text = device.label ?: device.credentialId.take(12),
                    style = MaterialTheme.typography.bodyMedium,
                    fontWeight = FontWeight.SemiBold,
                    color = Palette.textBright,
                )
                if (isCurrent) {
                    Spacer(modifier = Modifier.width(6.dp))
                    Text(
                        text = "(THIS DEVICE)",
                        style = MaterialTheme.typography.labelSmall,
                        color = Palette.accent,
                        fontWeight = FontWeight.Bold,
                    )
                }
            }
            Text(
                text = "ID: ${device.credentialId.take(8)}...",
                style = MaterialTheme.typography.labelSmall,
                fontFamily = FontFamily.Monospace,
                color = Palette.muted,
            )
        }
        if (device.revoked) {
            Box(
                modifier = Modifier
                    .background(Palette.alert.copy(alpha = 0.2f), RoundedCornerShape(4.dp))
                    .padding(horizontal = 6.dp, vertical = 2.dp),
            ) {
                Text(
                    text = "REVOKED",
                    style = MaterialTheme.typography.labelSmall,
                    color = Palette.alert,
                    fontWeight = FontWeight.Bold,
                )
            }
        } else {
            Box(
                modifier = Modifier
                    .background(Palette.ok.copy(alpha = 0.2f), RoundedCornerShape(4.dp))
                    .padding(horizontal = 6.dp, vertical = 2.dp),
            ) {
                Text(
                    text = "ACTIVE",
                    style = MaterialTheme.typography.labelSmall,
                    color = Palette.ok,
                    fontWeight = FontWeight.Bold,
                )
            }
        }
    }
}

@Composable
private fun customTextFieldColors() = OutlinedTextFieldDefaults.colors(
    focusedBorderColor = Palette.accent,
    unfocusedBorderColor = Palette.surfaceBorder,
    focusedLabelColor = Palette.accent,
    unfocusedLabelColor = Palette.muted,
    focusedTextColor = Palette.textBright,
    unfocusedTextColor = Palette.text,
    cursorColor = Palette.accent,
    focusedContainerColor = Palette.surfaceInset,
    unfocusedContainerColor = Palette.surfaceInset,
)

private fun defaultDeviceLabel(): String {
    val model = Build.MODEL
    val manufacturer = Build.MANUFACTURER
    return if (model.startsWith(manufacturer, ignoreCase = true)) {
        model.replaceFirstChar { it.uppercase() }
    } else {
        "${manufacturer.replaceFirstChar { it.uppercase() }} $model"
    }
}
