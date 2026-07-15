package ai.ironagent.app.ui

import androidx.compose.runtime.Composable
import ai.ironagent.app.MainViewModel
import ai.ironagent.app.ui.chat.ChatSheetContent

@Composable
fun ChatSheet(viewModel: MainViewModel) {
  ChatSheetContent(viewModel = viewModel)
}
