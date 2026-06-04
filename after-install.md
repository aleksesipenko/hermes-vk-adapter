# Hermes VK Adapter Installed

The VK platform plugin is installed and enabled.

Read the Russian setup guide before starting the gateway:

https://github.com/aleksesipenko/hermes-vk-adapter#readme

Next checks:

```bash
hermes plugins list
hermes gateway status
```

VK setup checklist:

1. Use a VK community access token, not a personal VK user token.
2. Enable community messages.
3. Enable Long Poll API events: `message_new` and `message_event`.
4. Enable Bot features / Chat bot feature in the VK community settings.
5. Set `VK_ALLOWED_USERS` to the numeric VK user ids allowed to talk to Hermes.
6. Set `VK_HOME_PEER_ID` after the first Long Poll message reveals the real peer id.

Restart the gateway after changing plugin or environment settings:

```bash
hermes gateway restart
```
