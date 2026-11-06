import { Tabs } from "expo-router/js-tabs";

import { usePalette } from "../../components/ui";

export default function TabsLayout() {
  const c = usePalette();
  return (
    <Tabs
      screenOptions={{
        tabBarActiveTintColor: c.accent,
        tabBarIcon: () => null,
        tabBarLabelStyle: { fontSize: 14, fontWeight: "600" },
        tabBarStyle: { backgroundColor: c.card, borderTopColor: c.line },
        headerStyle: { backgroundColor: c.card },
        headerTintColor: c.text,
      }}
    >
      <Tabs.Screen name="index" options={{ title: "Today" }} />
      <Tabs.Screen name="inbox" options={{ title: "Replies" }} />
      <Tabs.Screen name="dms" options={{ title: "DMs" }} />
      <Tabs.Screen name="settings" options={{ title: "Settings" }} />
    </Tabs>
  );
}
