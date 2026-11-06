import { router, useLocalSearchParams } from "expo-router";
import { useMemo, useState } from "react";
import { ScrollView } from "react-native";

import { Body, Button, Card, ErrorText, Field, Row, Screen, Title } from "../components/ui";
import { useSettings } from "../lib/settings";

const URL_RE = /https?:\/\/\S+/i;
const EMAIL_RE = /[\w.+-]+@[\w-]+(\.[\w-]+)+/;

/** Shared text often wraps the link ("Check out @x on TikTok! https://vm.tiktok.com/..."). */
function extractUrl(shared: string): string {
  const m = shared.match(URL_RE);
  return m ? m[0].replace(/[).,]+$/, "") : shared.trim();
}

export default function CaptureScreen() {
  const { shared = "" } = useLocalSearchParams<{ shared?: string }>();
  const { api, configured } = useSettings();
  const initial = useMemo(() => extractUrl(shared), [shared]);
  const [url, setUrl] = useState(initial);
  const [note, setNote] = useState("");
  const [email, setEmail] = useState("");
  const [result, setResult] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function saveCreator() {
    setBusy(true);
    setError(null);
    try {
      const typedEmail = email.trim() || note.match(EMAIL_RE)?.[0];
      const res = await api.capture({ url: url.trim(), note: note.trim() || undefined, email: typedEmail || undefined });
      setResult(`${res.message}: ${res.name}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Screen>
      <ScrollView contentContainerStyle={{ padding: 16 }} keyboardShouldPersistTaps="handled">
        <Card>
          <Title>Save a creator</Title>
          {!configured && <Body muted>Set up the backend in Settings first.</Body>}
          <Field label="Profile or video link" value={url} onChangeText={setUrl} autoCapitalize="none" autoCorrect={false} />
          <Field label="Business email (if you can see one)" value={email} onChangeText={setEmail} autoCapitalize="none"
                 keyboardType="email-address" />
          <Field label="Note (optional)" value={note} onChangeText={setNote} multiline />
          {result ? (
            <>
              <Body>{result}</Body>
              <Row><Button title="Done" onPress={() => router.back()} /></Row>
            </>
          ) : (
            <Row>
              <Button title="Save" busy={busy} disabled={!configured || !url.trim()} onPress={saveCreator} />
              <Button title="Cancel" kind="secondary" onPress={() => router.back()} />
            </Row>
          )}
          <ErrorText error={error} />
        </Card>
      </ScrollView>
    </Screen>
  );
}
