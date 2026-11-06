import { router } from "expo-router";
import { FlatList, Pressable, RefreshControl } from "react-native";

import { Body, Card, ErrorText, Screen, Title } from "../../components/ui";
import { useSettings } from "../../lib/settings";
import { useLoad } from "../../lib/useLoad";

export default function InboxScreen() {
  const { api } = useSettings();
  const { data, error, refreshing, reload } = useLoad(() => api.inbox(), [api]);

  return (
    <Screen>
      <FlatList
        contentContainerStyle={{ padding: 16 }}
        data={data ?? []}
        keyExtractor={(m) => String(m.id)}
        refreshControl={<RefreshControl refreshing={refreshing} onRefresh={reload} />}
        ListHeaderComponent={<ErrorText error={error} />}
        ListEmptyComponent={error ? undefined : <Body muted>No replies yet.</Body>}
        renderItem={({ item }) => (
          <Pressable
            onPress={() => {
              if (!item.read) api.markRead(item.id).catch(() => undefined);
              if (item.outreach_id) router.push(`/thread/${item.outreach_id}`);
            }}
          >
            <Card>
              <Title>{item.read ? "" : "● "}{item.person?.name ?? item.from_name ?? item.from}</Title>
              <Body muted small>
                {new Date(item.received_at).toLocaleString()}{item.opt_out ? " · asked to opt out" : ""}
              </Body>
              <Body small>{item.snippet}</Body>
            </Card>
          </Pressable>
        )}
      />
    </Screen>
  );
}
