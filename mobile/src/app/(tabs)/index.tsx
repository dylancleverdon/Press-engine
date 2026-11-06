import { router } from "expo-router";
import { useState } from "react";
import { Alert, RefreshControl, ScrollView, View } from "react-native";

import { Body, Button, Card, ErrorText, Row, Screen, Stat, Title } from "../../components/ui";
import { useSettings } from "../../lib/settings";
import type { Outreach } from "../../lib/types";
import { useLoad } from "../../lib/useLoad";

function time(iso: string | null) {
  return iso ? new Date(iso).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" }) : "";
}

export default function TodayScreen() {
  const { api, configured } = useSettings();
  const { data, error, refreshing, reload } = useLoad(() => api.today(), [api]);
  const [busy, setBusy] = useState<number | null>(null);

  if (!configured) {
    return (
      <Screen>
        <View style={{ padding: 16 }}>
          <Card>
            <Title>Welcome</Title>
            <Body>Add the backend address and your token in Settings to get started.</Body>
            <Row><Button title="Open settings" onPress={() => router.push("/settings")} /></Row>
          </Card>
        </View>
      </Screen>
    );
  }

  async function act(o: Outreach, action: "approve" | "skip") {
    setBusy(o.id);
    try {
      if (action === "approve") await api.approve(o.id);
      else await api.skip(o.id);
      await reload();
    } catch (e) {
      Alert.alert("Couldn't update", e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  return (
    <Screen>
      <ScrollView contentContainerStyle={{ padding: 16 }} refreshControl={<RefreshControl refreshing={refreshing} onRefresh={reload} />}>
        <ErrorText error={error} />
        {data && (
          <>
            {data.warnings.map((w) => (
              <Body key={w} muted small>⚠︎ {w}</Body>
            ))}
            <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 8, marginVertical: 12 }}>
              <Stat value={`${data.sent_today}/${data.cap}`} label="emails sent / cap" />
              <Stat value={data.awaiting_approval.length} label="to approve" />
              <Stat value={data.scheduled.length} label="scheduled" />
              <Stat value={data.unread_replies} label="new replies" />
              <Stat value={data.dm_queue} label="DMs to send" />
              <Stat value={data.new_leads_24h} label="new leads (24h)" />
            </View>

            {data.awaiting_approval.length > 0 && <Title>Waiting for approval</Title>}
            {data.awaiting_approval.map((o) => (
              <Card key={o.id}>
                <Title>{o.person.name}</Title>
                <Body muted small>{o.person.outlet ?? o.person.kind} · {o.to} · taste {Math.round(o.person.taste_score * 100)}</Body>
                <Body small>{o.subject}</Body>
                <Body muted small>{(o.body ?? "").slice(0, 280)}{(o.body ?? "").length > 280 ? "…" : ""}</Body>
                <Row>
                  <Button title="Approve" busy={busy === o.id} onPress={() => act(o, "approve")} />
                  <Button title="Skip" kind="danger" disabled={busy === o.id} onPress={() => act(o, "skip")} />
                </Row>
              </Card>
            ))}

            <Title>Scheduled today</Title>
            {data.scheduled.length === 0 && <Body muted>Nothing scheduled.</Body>}
            {data.scheduled.map((o) => (
              <Card key={o.id}>
                <Body>{time(o.scheduled_at) || "waiting for a slot"} · {o.person.name}</Body>
                <Body muted small>{o.subject}</Body>
              </Card>
            ))}

            <Title>Sent in the last 24 hours</Title>
            {data.sent.length === 0 && <Body muted>Nothing yet.</Body>}
            {data.sent.map((o) => (
              <Card key={o.id}>
                <Body>{time(o.sent_at)} · {o.person.name}</Body>
                <Body muted small>{o.status}</Body>
              </Card>
            ))}
          </>
        )}
      </ScrollView>
    </Screen>
  );
}
