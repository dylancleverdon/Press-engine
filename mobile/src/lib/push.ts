import Constants from "expo-constants";
import * as Device from "expo-device";
import * as Notifications from "expo-notifications";
import { Platform } from "react-native";

import type { ApiClient } from "./api";

Notifications.setNotificationHandler({
  handleNotification: async () => ({
    shouldShowBanner: true,
    shouldShowList: true,
    shouldPlaySound: true,
    shouldSetBadge: false,
  }),
});

function projectId(): string | undefined {
  const fromConfig = (Constants.expoConfig?.extra as { eas?: { projectId?: string } } | undefined)?.eas?.projectId;
  return fromConfig || Constants.easConfig?.projectId || undefined;
}

/** Ask for permission, get this phone's Expo push token and register it with the backend. */
export async function registerForReplyNotifications(api: ApiClient): Promise<string> {
  if (!Device.isDevice) throw new Error("Push notifications need a real phone, not a simulator.");
  if (Platform.OS === "android") {
    await Notifications.setNotificationChannelAsync("replies", {
      name: "Replies",
      importance: Notifications.AndroidImportance.HIGH,
    });
  }
  let { status } = await Notifications.getPermissionsAsync();
  if (status !== "granted") {
    ({ status } = await Notifications.requestPermissionsAsync());
  }
  if (status !== "granted") throw new Error("Notifications are turned off for Press Engine in your phone's settings.");
  const id = projectId();
  if (!id) throw new Error("Set expo.extra.eas.projectId in app.json (run `npx eas-cli@latest init`).");
  const { data: token } = await Notifications.getExpoPushTokenAsync({ projectId: id });
  await api.registerDevice(token, Platform.OS);
  await api.setNotifications(true);
  return token;
}
