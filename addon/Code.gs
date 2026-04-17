/**
 * Wayonagio Gmail Add-on
 *
 * Adds language-specific draft buttons when viewing an email in Gmail.
 * Clicking a button calls POST /draft-reply on the backend server.
 *
 * Setup (run once per deployment):
 *   1. Open this project in Apps Script editor.
 *   2. Go to Project Settings → Script Properties and add:
 *        BACKEND_URL   — e.g. https://your-server.example.com
 *        BEARER_TOKEN  — same value as AUTH_BEARER_TOKEN in your .env
 *   3. Deploy as a Google Workspace Add-on and install for your domain.
 */

/**
 * Builds the Add-on card shown when an email is open.
 * @param {Object} e - Gmail contextual event object.
 * @returns {Card}
 */
function buildAddOn(e) {
  var messageId = e.gmail.messageId;
  var threadId = e.gmail.threadId;

  return buildDraftButtonsCard(messageId, threadId);
}

/**
 * Builds the initial card with the two language draft buttons.
 * @param {string} messageId
 * @param {string} threadId
 * @returns {Card}
 */
function buildDraftButtonsCard(messageId, threadId) {
  var italianButton = CardService.newTextButton()
    .setText("🇮🇹 Draft in Italian")
    .setOnClickAction(
      CardService.newAction()
        .setFunctionName("onDraftReply")
        .setParameters({
          messageId: messageId,
          threadId: threadId,
          language: "it",
        })
    );

  var spanishButton = CardService.newTextButton()
    .setText("🇵🇪 Draft in Spanish")
    .setOnClickAction(
      CardService.newAction()
        .setFunctionName("onDraftReply")
        .setParameters({
          messageId: messageId,
          threadId: threadId,
          language: "es",
        })
    );

  var section = CardService.newCardSection()
    .setHeader("Wayonagio")
    .addWidget(italianButton)
    .addWidget(spanishButton);

  return CardService.newCardBuilder()
    .setHeader(CardService.newCardHeader().setTitle("Draft reply"))
    .addSection(section)
    .build();
}

/**
 * Builds the success card shown after a draft is created. Provides a button
 * that opens the Gmail thread so the user can see the new draft without
 * manually refreshing or navigating to the Drafts folder.
 * @param {string} draftId
 * @param {string} threadId
 * @param {string} messageId
 * @returns {Card}
 */
function buildSuccessCard(draftId, threadId, messageId) {
  var threadUrl = threadId
    ? "https://mail.google.com/mail/#all/" + encodeURIComponent(threadId)
    : "https://mail.google.com/mail/#drafts";

  var openThreadButton = CardService.newTextButton()
    .setText("Open thread in new tab")
    .setOpenLink(
      CardService.newOpenLink()
        .setUrl(threadUrl)
        .setOpenAs(CardService.OpenAs.FULL_SIZE)
        .setOnClose(CardService.OnClose.NOTHING)
    );

  var backButton = CardService.newTextButton()
    .setText("Back to draft buttons")
    .setOnClickAction(
      CardService.newAction()
        .setFunctionName("onBackToButtons")
        .setParameters({ messageId: messageId, threadId: threadId })
    );

  var section = CardService.newCardSection()
    .setHeader("Draft created")
    .addWidget(
      CardService.newTextParagraph().setText(
        "Your draft is ready in this thread. Click below to jump to it."
      )
    )
    .addWidget(openThreadButton)
    .addWidget(backButton)
    .addWidget(
      CardService.newTextParagraph().setText(
        "<font color=\"#888888\">Draft ID: " + draftId + "</font>"
      )
    );

  return CardService.newCardBuilder()
    .setHeader(CardService.newCardHeader().setTitle("Wayonagio"))
    .addSection(section)
    .build();
}

/**
 * Called when the user clicks a "Draft in <language>" button.
 * POSTs to the backend, then swaps the card for a success card with a button
 * that opens the Gmail thread.
 * @param {Object} e - Action event with parameters.messageId, threadId, language.
 * @returns {ActionResponse}
 */
function onDraftReply(e) {
  var messageId = e.parameters.messageId;
  var threadId = e.parameters.threadId;
  var language = e.parameters.language;
  var props = PropertiesService.getScriptProperties();
  var backendUrl = props.getProperty("BACKEND_URL");
  var bearerToken = props.getProperty("BEARER_TOKEN");

  if (!backendUrl || !bearerToken) {
    return CardService.newActionResponseBuilder()
      .setNotification(
        CardService.newNotification().setText(
          "Add-on not configured. Set BACKEND_URL and BEARER_TOKEN in Script Properties."
        )
      )
      .build();
  }

  var url = backendUrl.replace(/\/$/, "") + "/draft-reply";

  var options = {
    method: "post",
    contentType: "application/json",
    headers: { Authorization: "Bearer " + bearerToken },
    payload: JSON.stringify({ message_id: messageId, language: language }),
    muteHttpExceptions: true,
  };

  try {
    var response = UrlFetchApp.fetch(url, options);
    var code = response.getResponseCode();

    if (code === 200) {
      var body = JSON.parse(response.getContentText());
      var successCard = buildSuccessCard(body.draft_id, threadId, messageId);

      return CardService.newActionResponseBuilder()
        .setNotification(
          CardService.newNotification().setText("Draft created")
        )
        .setNavigation(CardService.newNavigation().updateCard(successCard))
        .build();
    } else {
      Logger.log("Backend error " + code + ": " + response.getContentText());
      return CardService.newActionResponseBuilder()
        .setNotification(
          CardService.newNotification().setText(
            "Error " + code + ": " + response.getContentText()
          )
        )
        .build();
    }
  } catch (err) {
    Logger.log("Request failed: " + err);
    return CardService.newActionResponseBuilder()
      .setNotification(
        CardService.newNotification().setText("Request failed: " + err)
      )
      .build();
  }
}

/**
 * Called from the success card's "Back to draft buttons" button.
 * Restores the initial card for the same message/thread.
 * @param {Object} e - Action event with parameters.messageId and threadId.
 * @returns {ActionResponse}
 */
function onBackToButtons(e) {
  var messageId = e.parameters.messageId;
  var threadId = e.parameters.threadId;
  var card = buildDraftButtonsCard(messageId, threadId);
  return CardService.newActionResponseBuilder()
    .setNavigation(CardService.newNavigation().updateCard(card))
    .build();
}
