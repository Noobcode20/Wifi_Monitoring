import { useCallback, useEffect, useState } from 'react';
import { ActivityIndicator, ScrollView, StyleSheet, View } from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import { ThemedText } from '@/components/ThemedText';
import { apiService } from '@/services/api.service';
import { useSettingsStore } from '@/stores/settingsStore';

type Status = { source_state: string; simulation_enabled: boolean; uptime_seconds: number; accepted_csi_packets: number; rejected_packets: number };
type Node = { node_id: number; csi_status: string; csi_last_seen_ms: number; rssi_dbm: number; packet_rate_hz: number };
type Snapshot = { status: Status; nodes: { nodes: Node[]; total: number }; latest: { measured_signal_change_pct: number | null } };

const ago = (ms: number) => (ms < 1000 ? 'now' : `${(ms / 1000).toFixed(1)}s ago`);

export const OverviewScreen = () => {
  const serverUrl = useSettingsStore((state) => state.serverUrl);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [online, setOnline] = useState(false);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      apiService.setBaseUrl(serverUrl);
      const [status, nodes, latest] = await Promise.all([
        apiService.get<Status>('/api/v1/status'),
        apiService.get<{ nodes: Node[]; total: number }>('/api/v1/nodes'),
        apiService.get<Snapshot['latest']>('/api/v1/sensing/latest'),
      ]);
      setSnapshot({ status, nodes, latest });
      setOnline(true);
    } catch {
      setOnline(false);
    } finally {
      setLoading(false);
    }
  }, [serverUrl]);

  useEffect(() => {
    void refresh();
    const timer = setInterval(() => void refresh(), 5000);
    return () => clearInterval(timer);
  }, [refresh]);

  const nodes = snapshot?.nodes.nodes ?? [];
  const fresh = nodes.filter((node) => node.csi_status === 'active' && node.csi_last_seen_ms < 5000);
  const meanRssi = fresh.length ? fresh.reduce((sum, node) => sum + node.rssi_dbm, 0) / fresh.length : null;

  return (
    <View style={styles.screen}>
      <ScrollView contentContainerStyle={styles.content}>
        <ThemedText style={styles.kicker}>RUView · LOCAL SYSTEM</ThemedText>
        <ThemedText style={styles.title}>Overview</ThemedText>
        <View style={[styles.connection, online ? styles.ok : styles.offline]}>
          <Ionicons name={online ? 'radio-button-on' : 'radio-button-off'} size={18} color={online ? '#08785a' : '#8a6a28'} />
          <View style={{ flex: 1 }}>
            <ThemedText style={styles.connectionTitle}>{online ? (fresh.length ? 'Receiver connected' : 'Receiver ready · no fresh CSI') : 'Receiver unavailable'}</ThemedText>
            <ThemedText style={styles.muted}>{online ? 'Local status refreshes every 5 seconds' : 'Check the server address in Settings'}</ThemedText>
          </View>
        </View>
        {loading && !snapshot && <ActivityIndicator color="#08785a" style={{ margin: 24 }} />}
        <View style={styles.metrics}>
          <View style={styles.metric}><ThemedText style={styles.label}>Fresh nodes</ThemedText><ThemedText style={styles.value}>{online ? `${fresh.length} / ${snapshot?.nodes.total ?? 4}` : '—'}</ThemedText></View>
          <View style={styles.metric}><ThemedText style={styles.label}>Mean RSSI</ThemedText><ThemedText style={styles.value}>{meanRssi === null ? '—' : `${meanRssi.toFixed(0)} dBm`}</ThemedText></View>
          <View style={styles.metric}><ThemedText style={styles.label}>Frame change</ThemedText><ThemedText style={styles.value}>{snapshot?.latest.measured_signal_change_pct == null ? '—' : `${snapshot.latest.measured_signal_change_pct.toFixed(1)}%`}</ThemedText></View>
        </View>
        <View style={styles.sectionHead}><ThemedText style={styles.sectionTitle}>Sensor nodes</ThemedText><ThemedText style={styles.muted}>Validated CSI links</ThemedText></View>
        {[1, 2, 3, 4].map((id) => {
          const node = nodes.find((item) => item.node_id === id);
          const active = node?.csi_status === 'active' && (node?.csi_last_seen_ms ?? Infinity) < 5000;
          return (
            <View key={id} style={styles.node}>
              <View style={[styles.dot, active ? styles.dotActive : styles.dotIdle]} />
              <View style={{ flex: 1 }}><ThemedText style={styles.nodeTitle}>Node {id}</ThemedText><ThemedText style={styles.muted}>{node ? `Last packet ${ago(node.csi_last_seen_ms)}` : 'No packet received'}</ThemedText></View>
              <ThemedText style={[styles.nodeValue, { color: active ? '#08785a' : '#78857e' }]}>{active ? `${node!.rssi_dbm} dBm` : node ? 'STALE' : 'WAITING'}</ThemedText>
            </View>
          );
        })}
        <ThemedText style={styles.foot}>{snapshot ? `${snapshot.status.accepted_csi_packets.toLocaleString()} valid packets · ${snapshot.status.rejected_packets} rejected · ${Math.floor(snapshot.status.uptime_seconds / 60)} min uptime` : 'Connect to the local RuView receiver to view measured system state.'}</ThemedText>
      </ScrollView>
    </View>
  );
};

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: '#f5f8f6' }, content: { padding: 20, paddingTop: 28, paddingBottom: 34 },
  kicker: { color: '#08785a', fontSize: 10, letterSpacing: 1.6, fontWeight: '700' }, title: { color: '#17221e', fontSize: 30, fontWeight: '700', marginTop: 5, marginBottom: 18 },
  connection: { padding: 14, borderRadius: 13, flexDirection: 'row', alignItems: 'center', gap: 11, marginBottom: 14 }, ok: { backgroundColor: '#e0eee8' }, offline: { backgroundColor: '#f5eddc' },
  connectionTitle: { color: '#25352e', fontSize: 14, fontWeight: '700' }, muted: { color: '#718078', fontSize: 11, marginTop: 3 },
  metrics: { flexDirection: 'row', gap: 8, marginBottom: 24 }, metric: { flex: 1, backgroundColor: '#fff', borderRadius: 13, padding: 12, minHeight: 82, borderWidth: 1, borderColor: '#e8eeea' },
  label: { color: '#718078', fontSize: 9, letterSpacing: 0.7, fontWeight: '700', textTransform: 'uppercase' }, value: { color: '#17221e', fontSize: 18, fontWeight: '700', marginTop: 8 },
  sectionHead: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 8 }, sectionTitle: { color: '#17221e', fontSize: 17, fontWeight: '700' },
  node: { backgroundColor: '#fff', borderRadius: 12, padding: 13, marginBottom: 7, flexDirection: 'row', alignItems: 'center', gap: 10, borderWidth: 1, borderColor: '#e8eeea' },
  dot: { width: 9, height: 9, borderRadius: 5 }, dotActive: { backgroundColor: '#08785a' }, dotIdle: { backgroundColor: '#bdc8c1' }, nodeTitle: { color: '#17221e', fontSize: 13, fontWeight: '700' }, nodeValue: { fontSize: 10, fontWeight: '700', letterSpacing: 0.5 },
  foot: { color: '#718078', fontSize: 11, lineHeight: 16, marginTop: 11 },
});

export default OverviewScreen;
