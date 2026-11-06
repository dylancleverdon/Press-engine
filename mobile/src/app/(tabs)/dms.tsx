import * as Clipboard from "expo-clipboard";
import { useState } from "react";
import { Alert, FlatList, Linking, RefreshControl } from "react-native";

import { Body, Button, Card, ErrorText, Field, Row, Screen, Title } from "../../components/ui";
import { useSettings } from "../../lib/settings";
import { useLoad } from "../../lib/useLoad";

export default function DmScreen() {
  const { api } = useSettings();
  const { data, error, refreshing, reload } = useLoad(() => api.dmQueue(), [api]);
  const [edits, setEdits] = useState<Record<number, string>>({});
  const [busy, setBusy] = useState<number | null>(null);

  async function markSent(id: number) {
    setBusy(id);
    try {
      await api.markDmSent(id, edits[id]);
      await reload();
    } catch (e) {
      Alert.alert("Couldn't mark as sent", e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  return (
    <Screen>
      <FlatList
        contentContainerStyle={{ padding: 16 }}
        data={data ?? []}
        keyExtractor={(o) => String(o.id)}
        refreshControl={<RefreshControl refreshing={refreshing} onRefresh={reload} />}
        ListHeaderComponent={
          <>
            <ErrorText error={error} />
            <Body muted small>Copy the message, open their profile, send it from the app, then mark it as sent.</Body>
          </>
        }
        ListEmptyComponent={error ? undefined : <Body muted>Nothing in the DM queue.</Body>}
        renderItem={({ item }) => {
          const text = edits[item.id] ?? item.body ?? "";
          return (
            <Card>
              <Title>{item.person.name}</Title>
              <Body muted small>{item.platform} · {item.to}{item.person.found_by ? ` · found by ${item.person.found_by}` : ""}</Body>
              <Field label="Message" multiline value={text} onChangeText={(t) => setEdits((e) => ({ ...e, [item.id]: t }))} />
              <Row>
                <Button title="Copy" kind="secondary" onPress={() => Clipboard.setStringAsync(text)} />
                {item.profile_url && (
                  <Button title={`Open ${item.platform}`} kind="secondary" onPress={() => Linking.openURL(item.profile_url!)} />
                )}
                <Button title="Mark sent" busy={busy === item.id} onPress={() => markSent(item.id)} />
              </Row>
            </Card>
          );
        }}
      />
    </Screen>
  );
}
