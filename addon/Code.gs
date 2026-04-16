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

  var italianButton = CardService.newTextButton()
    .setText("🇮🇹 Draft in Italian")
    .setOnClickAction(
      CardService.newAction()
        .setFunctionName("onDraftReply")
        .setParameters({ messageId: messageId, language: "it" })
    );

  var spanishButton = CardService.newTextButton()
    .setText("🇵🇪 Draft in Spanish")
    .setOnClickAction(
      CardService.newAction()
        .setFunctionName("onDraftReply")
        .setParameters({ messageId: messageId, language: "es" })
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
 * Called when the user clicks "Draft reply".
 * POSTs the message ID to the backend and shows a status notification.
 * @param {Object} e - Action event with parameters.messageId.
 * @returns {ActionResponse}
 */
function onDraftReply(e) {
  var messageId = e.parameters.messageId;
  var language = e.parameters.language;
  var props = PropertiesService.getScriptProperties();
  var backendUrl = props.getProperty("BACKEND_URL");
  var bearerToken = props.getProperty("BEARER_TOKEN");

  if (!backendUrl || !bearerToken) {
    return CardService.newActionResponseBuilder()
      .setNotification(
        CardService.newNotification()
          .setText("Add-on not configured. Set BACKEND_URL and BEARER_TOKEN in Script Properties.")
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
      return CardService.newActionResponseBuilder()
        .setNotification(
          CardService.newNotification()
            .setText("Draft created (id: " + body.draft_id + ")")
        )
        .build();
    } else {
      Logger.log("Backend error " + code + ": " + response.getContentText());
      return CardService.newActionResponseBuilder()
        .setNotification(
          CardService.newNotification()
            .setText("Error " + code + ": " + response.getContentText())
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
