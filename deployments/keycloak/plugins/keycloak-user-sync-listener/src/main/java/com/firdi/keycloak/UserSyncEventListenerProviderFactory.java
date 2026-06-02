package com.firdi.keycloak;

import org.keycloak.Config;
import org.keycloak.events.EventListenerProvider;
import org.keycloak.events.EventListenerProviderFactory;
import org.keycloak.models.KeycloakSession;
import org.keycloak.models.KeycloakSessionFactory;

public class UserSyncEventListenerProviderFactory implements EventListenerProviderFactory {

    private String webhookUrl;
    private String webhookSecret;

    @Override
    public EventListenerProvider create(KeycloakSession session) {
        return new UserSyncEventListenerProvider(webhookUrl, webhookSecret);
    }

    @Override
    public void init(Config.Scope config) {
        this.webhookUrl = System.getenv().getOrDefault(
            "USER_SYNC_WEBHOOK_URL",
            "http://host.docker.internal:8763/keycloak/user-sync"
        );

        this.webhookSecret = System.getenv().getOrDefault(
            "USER_SYNC_WEBHOOK_SECRET",
            "dev-only-secret"
        );

        System.out.println("[user-sync-listener] webhookUrl=" + webhookUrl);
    }

    @Override
    public void postInit(KeycloakSessionFactory factory) {
    }

    @Override
    public void close() {
    }

    @Override
    public String getId() {
        return "user-sync-listener";
    }
}