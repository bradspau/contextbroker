# Build context is the BBF_921_java checkout (TMF921_SRC). The licensed TIO ontology
# is deliberately NOT copied: it is bind-mounted read-only at /app/ontology at run time.
# Stage 1 - build
FROM maven:3.9-eclipse-temurin-21-jammy AS build
WORKDIR /app
COPY pom.xml .
RUN mvn dependency:resolve -q 2>&1 | tail -3 || true
COPY src/ src/
RUN mvn package -DskipTests -q

# Stage 2 - runtime
FROM eclipse-temurin:21-jre-jammy
WORKDIR /app
COPY --from=build /app/target/*.jar app.jar
COPY BBF_access/ BBF_access/
COPY seed_data/ seed_data/
RUN mkdir -p /app/ontology
ENV SPRING_PROFILES_ACTIVE=standalone
EXPOSE 8000
ENTRYPOINT ["java", "-jar", "app.jar"]
