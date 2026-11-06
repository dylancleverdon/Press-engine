import * as Notifications from "expo-notifications";
import { Stack, router } from "expo-router";
import { useIncomingShare } from "expo-sharing";
import { StatusBar } from "expo-status-bar";
import { useEffect, useRef } from "react";

import { SettingsProvider } from "../lib/settings";
import "../lib/push";

/** Share sheet: TikTok / Instagram -> Share -> Press Engine opens the capture screen. */
function ShareHandler() {
  const { sharedPayloads, clearSharedPayloads } = useIncomingShare();
  useEffect(() => {
    const first = sharedPayloads.find((p) => p.shareType === "url" || p.shareType === "text");
    if (!first) return;
    clearSharedPayloads();
    router.push({ pathname: "/capture", params: { shared: first.value } });
  }, [sharedPayloads, clearSharedPayloads]);
  return null;
}

/** Tapping a "X replied" notification opens that thread. */
function NotificationHandler() {
  const response = Notifications.useLastNotificationResponse();
  const handled = useRef<string | null>(null);
  useEffect(() => {
    if (!response || handled.current === response.notification.request.identifier) return;
    handled.current = response.notification.request.identifier;
    const data = response.notification.request.content.data as { outreach_id?: number } | undefined;
    if (data?.outreach_id) router.push(`/thread/${data.outreach_id}`);
  }, [response]);
  return null;
}

export default function RootLayout() {
  return (
    <SettingsProvider>
      <StatusBar style="auto" />
      <ShareHandler />
      <NotificationHandler />
      <Stack>
        <Stack.Screen name="(tabs)" options={{ headerShown: false }} />
        <Stack.Screen name="thread/[id]" options={{ title: "Conversation" }} />
        <Stack.Screen name="capture" options={{ title: "Save creator", presentation: "modal" }} />
      </Stack>
    </SettingsProvider>
  );
}
