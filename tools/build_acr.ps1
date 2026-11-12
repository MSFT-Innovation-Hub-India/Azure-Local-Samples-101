param(
    [Parameter(Mandatory = $true)]
    [string]$Registry,
    [string]$Subscription,
    [string]$AzureConfigDir,
    [string]$RepositoryPrefix = "assistant",
    [string]$Tag = ("pdf-assistant-" + (Get-Date -Format "yyyyMMddHHmmss")),
    [string]$OutputPath = ".local\acr-images.json"
)

$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent

function Assert-AzSuccess([string]$Step) {
    if ($LASTEXITCODE -ne 0) { throw "$Step failed. No deployment was performed." }
}

$scope = @()
if ($Subscription) { $scope = @('--subscription', $Subscription) }
$previousConfig = $env:AZURE_CONFIG_DIR
if ($AzureConfigDir) { $env:AZURE_CONFIG_DIR = (Resolve-Path $AzureConfigDir).Path }
Push-Location $root
try {
    $loginServer = az acr show --name $Registry @scope --query loginServer -o tsv
    Assert-AzSuccess 'Reading the approved registry'
    if ([string]::IsNullOrWhiteSpace($loginServer)) { throw 'Registry has no login server.' }

    az acr build --registry $Registry --file Dockerfile --target app `
        --image "${RepositoryPrefix}/app:$Tag" --platform linux/amd64 --no-logs @scope .
    Assert-AzSuccess 'Building the React/FastAPI/indexer image in ACR'

    az acr build --registry $Registry --file Dockerfile --target embeddings `
        --image "${RepositoryPrefix}/embeddings:$Tag" --platform linux/amd64 --no-logs @scope .
    Assert-AzSuccess 'Building the offline embedding image in ACR'

    az acr import --name $Registry `
        --source "docker.io/qdrant/qdrant@sha256:9bfc89f9914fc14876a88c05270685921a55b8877b254a98bcc1cc2f0a424cd2" `
        --image "${RepositoryPrefix}/qdrant:$Tag" @scope
    Assert-AzSuccess 'Mirroring the pinned Qdrant image into ACR'

    $appDigest = az acr repository show --name $Registry --image "${RepositoryPrefix}/app:$Tag" @scope --query digest -o tsv
    Assert-AzSuccess 'Resolving the application digest'
    $embeddingDigest = az acr repository show --name $Registry --image "${RepositoryPrefix}/embeddings:$Tag" @scope --query digest -o tsv
    Assert-AzSuccess 'Resolving the embedding digest'
    $qdrantDigest = az acr repository show --name $Registry --image "${RepositoryPrefix}/qdrant:$Tag" @scope --query digest -o tsv
    Assert-AzSuccess 'Resolving the Qdrant digest'
    $images = @{
        registry = $Registry
        repository_prefix = $RepositoryPrefix
        app_image = "$loginServer/$RepositoryPrefix/app@$appDigest"
        embeddings_image = "$loginServer/$RepositoryPrefix/embeddings@$embeddingDigest"
        qdrant_image = "$loginServer/$RepositoryPrefix/qdrant@$qdrantDigest"
    } | ConvertTo-Json
    $parent = Split-Path $OutputPath -Parent
    if ($parent) { New-Item -ItemType Directory -Force $parent | Out-Null }
    $images | Set-Content -Encoding ascii $OutputPath
    Write-Output $images
}
finally {
    Pop-Location
    $env:AZURE_CONFIG_DIR = $previousConfig
}
