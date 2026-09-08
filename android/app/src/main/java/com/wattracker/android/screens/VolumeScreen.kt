package com.wattracker.android.screens

import androidx.compose.runtime.Composable
import androidx.compose.ui.res.stringResource
import com.wattracker.android.R
import com.wattracker.android.shell.ScreenScaffold
import com.wattracker.android.shell.StubPanel

/**
 * Volume (#198): weekly hours, TSS, distance and calories.
 *
 * Stub for now -- #193 is the shell only; no network code in this step.
 */
@Composable
fun VolumeScreen() {
    ScreenScaffold(
        title = stringResource(R.string.destination_volume),
        subtitle = stringResource(R.string.screen_volume_subtitle),
    ) {
        StubPanel(
            note = stringResource(R.string.stub_volume_note),
            issue = stringResource(R.string.stub_issue_volume),
        )
    }
}
