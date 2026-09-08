package com.wattracker.android.screens

import androidx.compose.runtime.Composable
import androidx.compose.ui.res.stringResource
import com.wattracker.android.R
import com.wattracker.android.shell.ScrollableScreenScaffold
import com.wattracker.android.shell.StubPanel

/**
 * Settings (#195): backend pairing (cloud / local), device state, key type.
 *
 * Stub for now -- #193 is the shell only; pairing arrives with the client
 * work.
 */
@Composable
fun SettingsScreen() {
    ScrollableScreenScaffold(
        title = stringResource(R.string.destination_settings),
        subtitle = stringResource(R.string.screen_settings_subtitle),
    ) {
        StubPanel(
            note = stringResource(R.string.stub_settings_note),
            issue = stringResource(R.string.stub_issue_settings),
        )
    }
}
