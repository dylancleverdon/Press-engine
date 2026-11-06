import { Stack, useLocalSearchParams } from "expo-router";
import { useState } from "react";
import { Alert, KeyboardAvoidingView, Platform, ScrollView, View } from "react-native";

import { Body, Button, Card, ErrorText, Field, Row, Screen, usePalette } from "../../components/ui";
import { useSettings } from "../../lib/settings";
import { useLoad } from "../../lib/useLoad";

export default function ThreadScreen() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const outreachId = Number(id);
  const { api } = useSettings();
  const c = usePalette();
  const { data, error, reload } = useLoad(() => api.thread(outreachId), [api, outreachId]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState<"draft" | "send" | null>(null);

  async function suggest() {
    setBusy("draft");
    try {
      const res = await api.draft(outreachId);
      setDraft(res.draft);
    } catch (e) {
      Alert.alert("No draft", e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  function send() {
    Alert.alert("Send this reply?", "It goes out from your Gmail, in the same thread.", [
      { text: "Cancel", style: "cancel" },
      {
        text: "Send",
        onPress: async () => {
          setBusy("send");
          try {
            await api.reply(outreachId, draft);
            setDraft("");
            await reload();
          } catch (e) {
            Alert.alert("Not sent", e instanceof Error ? e.message : String(e));
          } finally {
            setBusy(null);
          }
        },
      },
    ]);
  }

  return (
    <Screen>
      <Stack.Screen options={{ title: data?.outreach.person.name ?? "Conversation" }} />
      <KeyboardAvoidingView style={{ flex: 1 }} behavior={Platform.OS === "ios" ? "padding" : undefined} keyboardVerticalOffset={90}>
        <ScrollView contentContainerStyle={{ padding: 16 }} keyboardShouldPersistTaps="handled">
          <ErrorText error={error} />
          {data?.messages.map((m) => (
            <View key={m.id} style={{ borderLeftWidth: 3, borderLeftColor: m.is_me ? c.accent : c.line, paddingLeft: 10, marginBottom: 14 }}>
              <Body muted small>{m.is_me ? "You" : m.from_name || m.from_address} · {new Date(m.date).toLocaleString()}</Body>
              <Body>{m.text}</Body>
            </View>
          ))}
          <Card>
            <Field label="Your reply" multiline value={draft} onChangeText={setDraft} placeholder="Write a reply, or get a suggested draft and make it yours." />
            <Row>
              <Button title="Suggest a draft" kind="secondary" busy={busy === "draft"} onPress={suggest} />
              <Button title="Send" busy={busy === "send"} disabled={!draft.trim()} onPress={send} />
            </Row>
            <Body muted small>Drafts are suggestions only. Nothing is sent until you tap Send.</Body>
          </Card>
        </ScrollView>
      </KeyboardAvoidingView>
    </Screen>
  );
}
