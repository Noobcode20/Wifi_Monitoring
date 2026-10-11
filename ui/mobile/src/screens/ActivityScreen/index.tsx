import { useCallback, useEffect, useRef, useState } from 'react';
import { ActivityIndicator, Alert, ScrollView, StyleSheet, View } from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import { ThemedText } from '@/components/ThemedText';
import { useSettingsStore } from '@/stores/settingsStore';
import { apiService } from '@/services/api.service';

type SystemEvent = { id: number; created_at: string; severity: string; title: string; message: string; source: string };

export const ActivityScreen = () => {
  const serverUrl = useSettingsStore((state) => state.serverUrl);
  const [events, setEvents] = useState<SystemEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [online, setOnline] = useState(false);
  const seen = useRef<number | null>(null);

  const refresh = useCallback(async () => {
    try {
      apiService.setBaseUrl(serverUrl);
      const response = await apiService.get<{ events: SystemEvent[] }>('/api/v1/events');
      const next = Array.isArray(response.events) ? response.events : [];
      const newest = next[0];
      if (seen.current !== null && newest && newest.id > seen.current && newest.severity !== 'info') {
        Alert.alert(newest.title, newest.message);
      }
      if (newest) seen.current = Math.max(seen.current ?? newest.id, newest.id);
      setEvents(next);
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

  return (
    <View style={styles.screen}>
      <ScrollView contentContainerStyle={styles.content}>
        <ThemedText style={styles.kicker}>RUView · SYSTEM ACTIVITY</ThemedText>
        <ThemedText style={styles.title}>Notifications</ThemedText>
        <View style={[styles.status, online ? styles.online : styles.offline]}>
          <Ionicons name={online ? 'radio-button-on' : 'radio-button-off'} size={17} color={online ? '#08785a' : '#8a6a28'} />
          <ThemedText style={styles.statusText}>{online ? 'Connected to your local receiver' : 'Receiver unavailable · retrying'}</ThemedText>
        </View>
        {loading && <ActivityIndicator color="#08785a" style={{ margin: 20 }} />}
        {!loading && online && events.length === 0 && (
          <View style={styles.empty}>
            <Ionicons name="checkmark-circle" size={30} color="#08785a" />
            <ThemedText style={styles.emptyTitle}>All clear</ThemedText>
            <ThemedText style={styles.copy}>System events from the receiver will appear here.</ThemedText>
          </View>
        )}
        {!online && <ThemedText style={styles.copy}>Set the server address in Settings. On a phone, use your computer’s local network address and port 3002.</ThemedText>}
        {events.map((event) => {
          const warning = event.severity === 'warning' || event.severity === 'error';
          return (
            <View key={event.id} style={styles.event}>
              <View style={[styles.icon, warning ? styles.warnIcon : styles.okIcon]}>
                <Ionicons name={warning ? 'warning' : 'checkmark'} size={17} color={warning ? '#9a6a00' : '#08785a'} />
              </View>
              <View style={{ flex: 1 }}>
                <View style={styles.eventHead}>
                  <ThemedText style={styles.eventTitle}>{event.title}</ThemedText>
                  <ThemedText style={styles.time}>{new Date(event.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</ThemedText>
                </View>
                <ThemedText style={styles.copy}>{event.message}</ThemedText>
              </View>
            </View>
          );
        })}
      </ScrollView>
    </View>
  );
};

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: '#f5f8f6' },
  content: { padding: 20, paddingTop: 28, paddingBottom: 40 },
  kicker: { color: '#08785a', fontSize: 10, letterSpacing: 1.6, fontWeight: '700' },
  title: { color: '#17221e', fontSize: 30, fontWeight: '700', marginTop: 5, marginBottom: 18 },
  status: { padding: 13, borderRadius: 12, flexDirection: 'row', alignItems: 'center', gap: 9, marginBottom: 18 },
  online: { backgroundColor: '#e0eee8' }, offline: { backgroundColor: '#f5eddc' },
  statusText: { color: '#26352e', fontSize: 13, fontWeight: '600' },
  event: { backgroundColor: '#fff', borderRadius: 14, padding: 14, marginBottom: 10, flexDirection: 'row', gap: 11, borderWidth: 1, borderColor: '#e8eeea' },
  icon: { width: 34, height: 34, borderRadius: 10, alignItems: 'center', justifyContent: 'center' },
  okIcon: { backgroundColor: '#e2f4ed' }, warnIcon: { backgroundColor: '#fff2d5' },
  eventHead: { flexDirection: 'row', justifyContent: 'space-between', gap: 8, alignItems: 'center' },
  eventTitle: { color: '#17221e', fontSize: 14, fontWeight: '700', flex: 1 },
  time: { color: '#718078', fontSize: 11 },
  copy: { color: '#5d6d64', fontSize: 12, lineHeight: 18, marginTop: 4 },
  empty: { alignItems: 'center', padding: 28, borderRadius: 14, backgroundColor: '#fff' },
  emptyTitle: { color: '#17221e', fontWeight: '700', fontSize: 17, marginTop: 8 },
});

export default ActivityScreen;
