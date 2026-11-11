# Build context: this directory (only holds this Dockerfile). The BBF_921_java checkout
# (TMF921_SRC) is the named context "bbf"; only the explicit COPY --from=bbf sources below
# are pulled from it. The licensed TIO ontology is deliberately NOT copied: it is
# bind-mounted read-only at /app/ontology at run time.
# Stage 1 - build
FROM maven:3.9-eclipse-temurin-21-jammy@sha256:7f8422bd81f19b9035007a75e2f82272b09af64db08848d7d4e93302466e8b6f AS build
WORKDIR /app
COPY --from=bbf pom.xml .
RUN mvn dependency:resolve -q
COPY --from=bbf src/ src/
RUN mvn package -DskipTests -q

# Stage 2 - runtime
FROM eclipse-temurin:21-jre-jammy@sha256:1eac8b8fee11ca7d6725a48bc5d41992ed1561504ebae25490d2d062847e8269
WORKDIR /app
COPY --from=build /app/target/*.jar app.jar
COPY --from=bbf BBF_access/ BBF_access/
COPY --from=bbf seed_data/ seed_data/
RUN mkdir -p /app/ontology
ENV SPRING_PROFILES_ACTIVE=standalone
EXPOSE 8000
ENTRYPOINT ["java", "-jar", "app.jar"]
